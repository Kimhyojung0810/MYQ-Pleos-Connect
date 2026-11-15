package ai.roadpod.app.speech

import ai.pleos.playground.stt.SpeechToText
import ai.pleos.playground.stt.constant.ErrorType
import ai.pleos.playground.stt.constant.Mode
import ai.pleos.playground.stt.listener.OnInitializeListener
import ai.pleos.playground.stt.listener.ResultListener
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import java.util.Locale

/**
 * Voice input. Pleos SpeechToText (ON_DEVICE) first; if it cannot start (not installed on this image,
 * init error, request error) the app falls back to Android's built-in SpeechRecognizer (ko-KR), so the
 * driver can still talk to 큐큐. Only FINAL results are delivered (contract: is_final=true only).
 * Listening starts from the mic button, after a line that asks something (dj.speak listen_after), or the debug trigger.
 */
class SpeechInput(
    context: Context,
    private val onFinal: (String) -> Unit,
    private val onStatus: (String) -> Unit,
) {
    private val ctx = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val stt = runCatching { SpeechToText(context, Mode.ON_DEVICE) }.getOrNull()
    @Volatile var ready = false; private set
    @Volatile private var usingAndroid = false
    private var android: SpeechRecognizer? = null
    @Volatile private var released = false

    private val listener = object : ResultListener {
        override fun onUpdated(text: String, isFinal: Boolean) {
            if (isFinal && text.isNotBlank()) onFinal(text.trim())
        }
        override fun onUpdatedEpdData(p0: Long, p1: Long) {}
        override fun onStartedRecognition() { onStatus("stt.listening") }
        override fun onEndedRecognition() { onStatus("stt.ended") }
        override fun onError(errorType: ErrorType) { onStatus("stt.error: ${errorType.name}") }
        override fun onReady() {}
        override fun onUpdatedRms(rms: Double) {}
    }

    fun start() {
        val pleos = stt ?: run { useAndroid("pleos_missing"); return }
        runCatching {
            pleos.initialize(object : OnInitializeListener {
                override fun onSuccess() {
                    pleos.addListener(listener)
                    runCatching { pleos.setLocale(Locale.KOREA) }
                    ready = true
                    onStatus("stt.ready(PLEOS_ON_DEVICE)")
                }
                override fun onFailure(errorType: ErrorType) {
                    onStatus("stt.init_failed: ${errorType.name}")
                    useAndroid("pleos_init_${errorType.name}")
                }
            })
        }.onFailure {
            onStatus("stt.init_exception: ${it.javaClass.simpleName} ${it.message}")
            useAndroid("pleos_exception")
        }
    }

    // ---------------- fallback: Android SpeechRecognizer ----------------

    private val androidListener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) { onStatus("stt.listening") }
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onError(error: Int) { onStatus("stt.error: ANDROID_$error"); onStatus("stt.ended") }
        override fun onResults(results: Bundle?) {
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
            onStatus("stt.ended")
            if (!text.isNullOrBlank()) onFinal(text.trim())
        }
        override fun onPartialResults(partialResults: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun useAndroid(reason: String) = main.post {
        if (usingAndroid || released) return@post
        if (!SpeechRecognizer.isRecognitionAvailable(ctx)) {
            ready = false
            onStatus("stt.unavailable ($reason; no Android recognizer on this device)")
            return@post
        }
        android = runCatching { SpeechRecognizer.createSpeechRecognizer(ctx).apply { setRecognitionListener(androidListener) } }.getOrNull()
        usingAndroid = android != null
        ready = usingAndroid
        onStatus(if (usingAndroid) "stt.ready(ANDROID fallback: $reason)" else "stt.unavailable ($reason)")
    }

    private fun androidListen() = main.post {
        val rec = android ?: return@post
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "ko-KR")
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
            .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        runCatching { rec.cancel(); rec.startListening(intent) }.onFailure { onStatus("stt.request_failed(ANDROID): ${it.javaClass.simpleName}") }
    }

    fun listenOnce(): Boolean {
        if (!ready) { onStatus("stt.not_ready"); return false }
        if (usingAndroid) { androidListen(); return true }
        val pleos = stt ?: return false
        return runCatching { pleos.request(); true }.getOrElse {
            onStatus("stt.request_failed: ${it.javaClass.simpleName} → Android fallback")
            useAndroid("pleos_request_failed")
            main.post { androidListen() }
            true
        }
    }

    fun stop() {
        runCatching { stt?.stop() }
        main.post { runCatching { android?.stopListening() } }
    }

    fun release() {
        released = true
        runCatching { stt?.removeListener(listener) }
        runCatching { stt?.release() }
        main.post { runCatching { android?.destroy() }; android = null }
    }
}
