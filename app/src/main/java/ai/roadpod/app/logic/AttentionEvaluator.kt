package ai.roadpod.app.logic

import ai.roadpod.app.vehicle.VehicleSignals
import kotlin.math.abs

/**
 * Uses the three NaviHelper speed values (road speed limit, congestion-ahead average speed,
 * speed-camera limit / section average) plus AAOS vehicle speed to flag segments
 * (decided 2026-11-11: "정차 구간, 운전자 주의 필요 구간 등에 활용").
 *
 * The numeric thresholds below are NOT decided yet. They are constructor parameters so the team
 * can set them; the defaults are placeholders and are listed in the pipeline report.
 * Results are used only inside the app (no schema field exists for them).
 */
class AttentionEvaluator(
    private val stoppedSpeedKmh: Double = 0.5,          // |speed| below this = stopped (sensor noise margin)
    private val cameraNearMeters: Int = 300,            // PLACEHOLDER (undecided)
    private val congestionNearMeters: Double = 300.0,   // PLACEHOLDER (undecided)
    private val congestionSlowKmh: Double = 20.0,       // PLACEHOLDER (undecided)
) {
    enum class Reason {
        VEHICLE_STOPPED,          // speed ~ 0
        CONGESTION_AHEAD_SLOW,    // slow congestion item close ahead
        SPEED_CAMERA_NEAR,        // camera alert within distance
        OVER_ROAD_SPEED_LIMIT,    // vehicle speed > road limit
        OVER_CAMERA_LIMIT,        // vehicle speed > camera limit
    }

    data class Result(val stoppedSegment: Boolean, val attentionRequired: Boolean, val reasons: Set<Reason>)

    fun evaluate(s: VehicleSignals): Result {
        val reasons = mutableSetOf<Reason>()
        val speed = s.speedKmh

        if (speed != null && abs(speed) < stoppedSpeedKmh) reasons += Reason.VEHICLE_STOPPED
        val cOffset = s.congestionAheadOffsetM
        val cAvg = s.congestionAheadAvgSpeedKmh
        if (cOffset != null && cAvg != null && cOffset <= congestionNearMeters && cAvg <= congestionSlowKmh) {
            reasons += Reason.CONGESTION_AHEAD_SLOW
        }
        val camDist = s.cameraRemainDistanceM
        if (camDist != null && camDist <= cameraNearMeters) reasons += Reason.SPEED_CAMERA_NEAR
        if (speed != null && s.roadSpeedLimitKmh != null && speed > s.roadSpeedLimitKmh) reasons += Reason.OVER_ROAD_SPEED_LIMIT
        if (speed != null && s.cameraSpeedLimitKmh != null && camDist != null && camDist <= cameraNearMeters &&
            speed > s.cameraSpeedLimitKmh
        ) reasons += Reason.OVER_CAMERA_LIMIT

        val stopped = Reason.VEHICLE_STOPPED in reasons || Reason.CONGESTION_AHEAD_SLOW in reasons
        val attention = reasons.any { it in ATTENTION }
        return Result(stopped, attention, reasons)
    }

    private companion object {
        val ATTENTION = setOf(Reason.SPEED_CAMERA_NEAR, Reason.OVER_ROAD_SPEED_LIMIT, Reason.OVER_CAMERA_LIMIT)
    }
}
