package ai.roadpod.app.vehicle

/** Raw, unmapped signals. null = not received / not supported. Never filled with guesses. */
data class VehicleSignals(
    // AAOS CarPropertyManager
    val speedKmh: Double? = null,            // PERF_VEHICLE_SPEED (m/s * 3.6); negative when reversing
    val speedSupported: Boolean? = null,
    val accelPedalPercent: Double? = null,   // ACCELERATOR_PEDAL_COMPRESSION_PERCENTAGE
    val accelSupported: Boolean? = null,
    val gearSelection: Int? = null,          // GEAR_SELECTION (VehicleGear int, PARK = 0x4)
    val gearSupported: Boolean? = null,
    // Pleos Vehicle SDK
    val brakePressed: Boolean? = null,
    val wiperOn: Boolean? = null,
    val gear: String? = null,                // collected for logging only (not used in decisions yet)
    // Pleos NaviHelper
    val navStatus: String? = null,           // INACTIVE / INITIALIZING / FREE_DRIVE / ROUTE_DRIVE
    val etaSeconds: Int? = null,
    val address: String? = null,
    val roadSpeedLimitKmh: Int? = null,      // onCurrentRoadSpeedLimit
    val congestionAheadAvgSpeedKmh: Double? = null, // TrafficCongestionInfo.avrSpeed (nearest item)
    val congestionAheadOffsetM: Double? = null,
    val congestionRoadName: String? = null,
    val cameraSpeedLimitKmh: Int? = null,    // nearest CameraAlert.speedLimit
    val cameraRemainDistanceM: Int? = null,
    val cameraSectionAvgSpeedKmh: Int? = null,
    val destinationArrivedAtMs: Long? = null,
    val simulated: Boolean = false,          // true when any value came from the debug simulator
    val speedLocked: Boolean = false,        // debug: simulated speed is not overwritten by the AAOS stream
    val gearLocked: Boolean = false,         // debug: simulated gear is not overwritten by the AAOS stream
)
