package ai.roadpod.app.speech

import android.media.AudioAttributes
import android.media.MediaPlayer

/**
 * 음악 코너 (C): plays a royalty-free track the server picked (music.play url). One track at a time.
 * Callbacks arrive on the main thread (MediaPlayer is created there).
 */
class MusicPlayer(private val onStatus: (String) -> Unit) {
    interface Callback {
        fun onStarted()
        fun onCompleted()
        fun onFailed(reason: String)
    }

    private var player: MediaPlayer? = null
    val isPlaying get() = player != null

    fun play(url: String, cb: Callback) {
        stop()
        val mp = MediaPlayer()
        player = mp
        runCatching {
            mp.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build()
            )
            mp.setDataSource(url)
            mp.setOnPreparedListener { if (player === it) { it.start(); cb.onStarted() } }
            mp.setOnCompletionListener { if (release(it)) cb.onCompleted() }
            mp.setOnErrorListener { m, what, extra -> if (release(m)) cb.onFailed("what=$what extra=$extra"); true }
            mp.prepareAsync()
        }.onFailure {
            onStatus("music.error ${it.javaClass.simpleName}")
            if (release(mp)) cb.onFailed(it.javaClass.simpleName)
        }
    }

    /** Stops without callbacks; the caller ACKs INTERRUPTED itself. */
    fun stop() {
        val mp = player ?: return
        runCatching { mp.stop() }
        release(mp)
    }

    /** True if [mp] was the current player (so its callback still matters). */
    private fun release(mp: MediaPlayer): Boolean {
        val current = player === mp
        if (current) player = null
        runCatching { mp.release() }
        return current
    }
}
