package ai.roadpod.app.speech

import ai.pleos.playground.tts.TextToSpeech
import ai.pleos.playground.tts.constant.ErrorType
import ai.pleos.playground.tts.constant.Mode
import ai.pleos.playground.tts.listener.EventListener
import ai.pleos.playground.tts.listener.OnInitializeListener
import android.content.Context
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Handler
import android.os.Looper
import android.speech.tts.UtteranceProgressListener
import java.util.Locale

/**
 * Pleos TextToSpeech wrapper, ON_DEVICE mode (no app registration / network needed per Pleos docs).
 * If Pleos TTS cannot start, the app falls back to Android's built-in TextToSpeech (Korean) so 큐큐 still talks.
 * One utterance at a time; the caller owns the queue.
 *
 * The SDK has no speech-rate setting, so pacing comes from pauses: a long line is spoken one sentence
 * at a time with [SENTENCE_GAP_MS] between them. The callback still sees one utterance
 * (onStarted once, then onCompleted / onInterrupted / onFailed once).
 *
 * When the server sends an `audio_url` (큐큐 voice made on the server, or `asset:` files bundled in the 체험 build), that mp3 is played instead.
 * If it fails before it starts, the same line is spoken with the on-device voice, so the show never goes silent.
 */
class DjSpeaker(
    context: Context,
    private val onStatus: (String) -> Unit,
) {
    private val ctx = context.applicationContext
    interface Callback {
        fun onStarted()
        fun onCompleted()
        fun onInterrupted()
        fun onFailed(reason: String)
    }

    private val tts = runCatching { TextToSpeech(context, Mode.ON_DEVICE) }.getOrNull()
    /** Fallback voice when Pleos TTS is not available on this image. */
    private var androidTts: android.speech.tts.TextToSpeech? = null
    @Volatile private var curUtt: String? = null   // Android TTS callbacks of an older utterance are ignored
    @Volatile private var released = false
    @Volatile var ready = false; private set

    // All state below is touched on the main thread only.
    private val main = Handler(Looper.getMainLooper())
    private var current: Callback? = null
    private val parts = ArrayDeque<String>()
    private var started = false
    private var inGap = false
    private var stopRequested = false
    private val nextPart = Runnable { inGap = false; speakNext() }
    private var player: MediaPlayer? = null      // server-made voice for the current line

    private val events = object : EventListener {
        override fun onStart() { main.post { val cb = current ?: return@post; if (!started) { started = true; cb.onStarted() } } }
        override fun onReady() {}
        override fun onDone() {
            main.post {
                if (current == null) return@post
                when {
                    stopRequested -> finish { it.onInterrupted() }
                    parts.isEmpty() -> finish { it.onCompleted() }
                    else -> { inGap = true; main.postDelayed(nextPart, SENTENCE_GAP_MS) }
                }
            }
        }
        override fun onError(errorType: ErrorType) { main.post { finish { it.onFailed("TTS_${errorType.name}") } } }
        override fun onStop() { main.post { finish { it.onInterrupted() } } }
        override fun onUpdatedRms(rms: Double) {}
    }

    private fun finish(report: (Callback) -> Unit) {
        val cb = current ?: return
        current = null; parts.clear(); inGap = false; stopRequested = false
        main.removeCallbacks(nextPart)
        releasePlayer()
        report(cb)
    }

    private fun releasePlayer() {
        val mp = player ?: return
        player = null
        runCatching { mp.stop() }
        runCatching { mp.release() }
    }

    /** Plays the server-made mp3; falls back to the on-device voice if it fails before it starts. */
    private fun playAudio(url: String, text: String) {
        val mp = MediaPlayer()
        player = mp
        val fallback = { reason: String ->
            if (player === mp && current != null) {
                onStatus("voice.fallback $reason")
                releasePlayer()
                if (started) finish { it.onFailed("VOICE_$reason") }    // broke mid-line: don't repeat it
                else if (!ready) finish { it.onFailed("VOICE_$reason TTS_NOT_READY") }
                else { parts.clear(); parts.addAll(sentences(text)); speakNext() }
            }
        }
        runCatching {
            mp.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            if (url.startsWith("asset:")) {   // 체험 build: voices bundled in the APK
                ctx.assets.openFd(url.removePrefix("asset:")).use { fd -> mp.setDataSource(fd.fileDescriptor, fd.startOffset, fd.length) }
            } else mp.setDataSource(url)
            mp.setOnPreparedListener {
                if (player !== it || current == null) return@setOnPreparedListener
                it.start()
                if (!started) { started = true; current?.onStarted() }
            }
            mp.setOnCompletionListener { if (player === it) finish { cb -> cb.onCompleted() } }
            mp.setOnErrorListener { _, what, extra -> fallback("what=$what extra=$extra"); true }
            mp.prepareAsync()
        }.onFailure { fallback(it.javaClass.simpleName) }
    }

    private fun speakNext() {
        val part = parts.removeFirstOrNull() ?: run { finish { it.onCompleted() }; return }
        val a = androidTts
        if (a != null) {
            val id = "qq-${System.nanoTime()}"; curUtt = id
            val r = runCatching { a.speak(part, android.speech.tts.TextToSpeech.QUEUE_FLUSH, null, id) }.getOrDefault(-1)
            if (r != android.speech.tts.TextToSpeech.SUCCESS) finish { it.onFailed("ANDROID_TTS_SPEAK_FAILED") }
            return
        }
        val p = tts ?: run { finish { it.onFailed("TTS_MISSING") }; return }
        runCatching { p.speak(part, true) }.onFailure { e -> finish { it.onFailed("TTS_EXCEPTION ${e.javaClass.simpleName}") } }
    }

    fun start() = runCatching { initialize() }.onFailure {
        ready = false
        onStatus("tts.init_exception: ${it.javaClass.simpleName} ${it.message}")
        useAndroidTts("pleos_exception")
    }

    private fun initialize() {
        val p = tts ?: run { useAndroidTts("pleos_missing"); return }
        p.initialize(object : OnInitializeListener {
            override fun onSuccess() {
                p.addEventListener(events)
                runCatching { p.setLocale(Locale.KOREA) }
                ready = true
                onStatus("tts.ready(PLEOS_ON_DEVICE)")
            }
            override fun onFailure(errorType: ErrorType) {
                ready = false
                onStatus("tts.init_failed: ${errorType.name}")
                useAndroidTts("pleos_init_${errorType.name}")
            }
        })
    }

    /** Android's built-in TTS, wired to the same callbacks as the Pleos engine. */
    private fun useAndroidTts(reason: String) = main.post {
        if (androidTts != null || released) return@post
        var engine: android.speech.tts.TextToSpeech? = null
        engine = android.speech.tts.TextToSpeech(ctx) { status ->
            main.post {
                val e = engine ?: return@post
                if (status != android.speech.tts.TextToSpeech.SUCCESS) { onStatus("tts.unavailable ($reason; Android TTS init $status)"); return@post }
                e.language = Locale.KOREAN
                e.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) { if (utteranceId == curUtt) events.onStart() }
                    override fun onDone(utteranceId: String?) { if (utteranceId == curUtt) events.onDone() }
                    @Deprecated("Deprecated in Java")
                    override fun onError(utteranceId: String?) { if (utteranceId == curUtt) main.post { finish { it.onFailed("ANDROID_TTS_ERROR") } } }
                    override fun onStop(utteranceId: String?, interrupted: Boolean) { if (utteranceId == curUtt) events.onStop() }
                })
                androidTts = e
                ready = true
                onStatus("tts.ready(ANDROID fallback: $reason)")
            }
        }
    }

    val isSpeaking: Boolean get() = current != null

    /** Call on the main thread. [audioUrl]: the server-made voice for [text], if any. */
    fun speak(text: String, cb: Callback, audioUrl: String? = null) {
        if (!ready && audioUrl == null) { cb.onFailed("TTS_NOT_READY"); return }
        current = cb; started = false; inGap = false; stopRequested = false
        parts.clear()
        if (audioUrl != null) { playAudio(audioUrl, text); return }
        parts.addAll(sentences(text))
        speakNext()
    }

    /** Immediate silence (navigation priority / media.pause). Reports onInterrupted. */
    fun stopNow() {
        if (current == null) return
        if (inGap || player != null) { finish { it.onInterrupted() }; return }
        stopRequested = true; parts.clear()
        val a = androidTts
        if (a != null) { curUtt = null; runCatching { a.stop() }; main.post { finish { it.onInterrupted() } }; return }
        runCatching { tts?.stop() }   // onStop (or a late onDone) finishes it
    }

    fun release() {
        released = true
        main.removeCallbacks(nextPart)
        releasePlayer()
        runCatching { tts?.removeEventListener(events) }
        runCatching { tts?.release() }
        runCatching { androidTts?.shutdown() }; androidTts = null
    }

    companion object {
        /** Pause between sentences of one line (the SDK has no speech-rate setting). */
        const val SENTENCE_GAP_MS = 450L
        private val SENTENCE = Regex("""[^.!?…]+[.!?…]*["'”’)]*\s*""")

        /** Same split as the screen's caption, so captions and speech change sentence together. */
        fun sentences(text: String): List<String> =
            SENTENCE.findAll(text).map { it.value.trim() }.filter { it.isNotEmpty() }.toList().ifEmpty { listOf(text) }
    }
}
