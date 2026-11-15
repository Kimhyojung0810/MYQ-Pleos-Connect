package ai.roadpod.app.debug

import ai.roadpod.app.RoadpodApp
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Debug, mock and self-contained release builds (adb only: the receiver requires DUMP). Drives scenarios on the emulator, because the Virtual Console cannot replay
 * navigation/location and the STT trigger is undecided. Values set here mark source_mode=SIMULATED.
 *
 *   adb shell am broadcast -n ai.roadpod.app/.debug.DebugReceiver -a ai.roadpod.debug.SAY --es text "큐큐야 사연 보낼래"
 *   adb shell am broadcast -n ai.roadpod.app/.debug.DebugReceiver -a ai.roadpod.debug.LISTEN
 *   adb shell am broadcast -n ai.roadpod.app/.debug.DebugReceiver -a ai.roadpod.debug.NAV --ez active true
 *   adb shell am broadcast -n ai.roadpod.app/.debug.DebugReceiver -a ai.roadpod.debug.SIM --ef speed 0 --ei eta 600 --ez brake false --ef accel 0 --ez arrive true
 *   (gear: VehicleGear int, 4 = P, 8 = D)  ... -a ai.roadpod.debug.SIM --ei gear 4
 *   adb shell am broadcast -n ai.roadpod.app/.debug.DebugReceiver -a ai.roadpod.debug.SHARE --ez on true
 *   adb shell am broadcast -n ai.roadpod.app/.debug.DebugReceiver -a ai.roadpod.debug.ACTION --es action skip_topic
 *   adb shell am broadcast -n ai.roadpod.app/.debug.DebugReceiver -a ai.roadpod.debug.ACTION --es action play_relay
 *   adb shell am broadcast -n ai.roadpod.app/.debug.DebugReceiver -a ai.roadpod.debug.STORY   (then SAY → sent as a story)
 */
class DebugReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val c = (context.applicationContext as RoadpodApp).controller
        when (intent.action) {
            "ai.roadpod.debug.SAY" -> intent.getStringExtra("text")?.let { c.onFinalUtterance(it) }
            "ai.roadpod.debug.LISTEN" -> c.listenOnce()
            "ai.roadpod.debug.NAV" -> c.setNavGuidance(intent.getBooleanExtra("active", false))
            "ai.roadpod.debug.ACTION" -> intent.getStringExtra("action")?.let { c.sendAction(it) }
            "ai.roadpod.debug.STORY" -> c.requestStory()
            "ai.roadpod.debug.SHARE" ->{ c.shareModeEnabled = intent.getBooleanExtra("on", false); c.status("share_mode=${c.shareModeEnabled}") }
            "ai.roadpod.debug.SIM" -> c.simulate { s ->
                var n = s
                if (intent.hasExtra("speed")) n = n.copy(speedKmh = intent.getFloatExtra("speed", 0f).toDouble(), speedLocked = true)
                if (intent.getBooleanExtra("unlock_speed", false)) n = n.copy(speedLocked = false)
                if (intent.hasExtra("accel")) n = n.copy(accelPedalPercent = intent.getFloatExtra("accel", 0f).toDouble(), accelSupported = true)
                if (intent.hasExtra("brake")) n = n.copy(brakePressed = intent.getBooleanExtra("brake", false))
                if (intent.hasExtra("eta")) n = n.copy(etaSeconds = intent.getIntExtra("eta", 0))
                if (intent.hasExtra("limit")) n = n.copy(roadSpeedLimitKmh = intent.getIntExtra("limit", 0))
                if (intent.hasExtra("camera_m")) n = n.copy(cameraRemainDistanceM = intent.getIntExtra("camera_m", 0))
                if (intent.hasExtra("gear")) n = n.copy(gearSelection = intent.getIntExtra("gear", 0), gearSupported = true, gearLocked = true)
                if (intent.getBooleanExtra("unlock_gear", false)) n = n.copy(gearLocked = false)
                if (intent.hasExtra("wiper")) n = n.copy(wiperOn = intent.getBooleanExtra("wiper", false))
                if (intent.getBooleanExtra("arrive", false)) n = n.copy(destinationArrivedAtMs = System.currentTimeMillis())
                if (intent.getBooleanExtra("clear_arrive", false)) n = n.copy(destinationArrivedAtMs = null)
                n
            }
        }
    }
}
