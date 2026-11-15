package ai.roadpod.app.logic

import ai.roadpod.app.contract.CommuteType
import ai.roadpod.app.contract.Congestion
import ai.roadpod.app.contract.MotionState
import ai.roadpod.app.contract.NavigationBlock
import ai.roadpod.app.contract.SourceMode
import ai.roadpod.app.contract.TrafficBlock
import ai.roadpod.app.contract.VehicleBlock
import ai.roadpod.app.contract.VehicleContextPayload
import ai.roadpod.app.contract.WeatherBlock
import ai.roadpod.app.contract.WeatherCondition
import ai.roadpod.app.vehicle.VehicleSignals
import kotlin.math.abs

/**
 * VehicleSignals → context.updated payload (schema file structure).
 * Fields whose mapping is undecided are sent as null / UNKNOWN / false and listed in the report:
 *  - commute_type, road_name, road_segment_id, traffic.*, weather.*, guidance_active
 *  - negative speed (reverse): schema has minimum 0 → sent as null until decided
 */
class ContextBuilder(
    private val stoppedSpeedKmh: Double = 0.5,
    private val commuteType: CommuteType = CommuteType.UNKNOWN,
) {
    fun build(s: VehicleSignals, parkedConfirmed: Boolean, runningOnEmulator: Boolean): VehicleContextPayload {
        val speed = s.speedKmh
        val motion = when {
            parkedConfirmed -> MotionState.PARKED
            speed == null -> MotionState.UNKNOWN
            abs(speed) < stoppedSpeedKmh -> MotionState.STOPPED
            else -> MotionState.MOVING
        }
        return VehicleContextPayload(
            commuteType = commuteType,
            vehicle = VehicleBlock(
                motionState = motion,
                speedKmh = speed?.takeIf { it >= 0.0 }?.let { Math.round(it * 10) / 10.0 },
                wiperActive = s.wiperOn,
                parkedConfirmed = parkedConfirmed,
            ),
            navigation = NavigationBlock(
                roadName = null,
                roadSegmentId = null,
                etaSeconds = s.etaSeconds?.takeIf { it >= 0 },
                guidanceActive = false,
            ),
            traffic = TrafficBlock(Congestion.UNKNOWN, verified = false),
            weather = WeatherBlock(WeatherCondition.UNKNOWN, verified = false),
            sourceMode = if (s.simulated || runningOnEmulator) SourceMode.SIMULATED else SourceMode.REAL,
        )
    }

    /** Send policy (undecided interval): send when the payload changes, at most every [minIntervalMs]. */
    class SendThrottle(private val minIntervalMs: Long = 2000L) {
        private var last: VehicleContextPayload? = null
        private var lastAt = 0L
        /** After a reconnect the server may have missed the latest context: allow an immediate resend. */
        fun forceNext() { last = null; lastAt = 0L }
        fun shouldSend(p: VehicleContextPayload, nowMs: Long): Boolean {
            if (p == last) return false
            if (nowMs - lastAt < minIntervalMs && last != null) return false
            last = p; lastAt = nowMs
            return true
        }
        /** Records a context sent outside [shouldSend] (e.g. the parked context) so it is not sent twice. */
        fun markSent(p: VehicleContextPayload, nowMs: Long) { last = p; lastAt = nowMs }
    }
}
