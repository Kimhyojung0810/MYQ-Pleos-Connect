package ai.roadpod.app

import android.app.Application
import android.provider.Settings

class RoadpodApp : Application() {
    private var _controller: RoadpodController? = null

    /** One controller per broadcast session. Recreated after the driver ends the broadcast ([releaseController]). */
    val controller: RoadpodController
        get() = _controller ?: RoadpodController(this, BuildConfig.ROADPOD_BASE_URL, BuildConfig.ROADPOD_DEV_TOKEN)
            .also { _controller = it }

    /** Sends session.ended, closes the WebSocket, releases SDKs, and drops the instance for a clean next start. */
    fun releaseController() {
        _controller?.stop()
        _controller = null
    }

    /** Pseudonymous device id for client_id (no personal data). */
    fun clientId(): String =
        "car-" + (Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown").take(12)
}
