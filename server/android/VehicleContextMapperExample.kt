// ROADPOD contract examples ONLY — not a buildable Pleos SDK adapter.
// Friend must replace raw SDK callbacks with the actual approved Pleos fields.
// The app owns real vehicle signal source, local interruption and physical playback status.
package ai.roadpod.contract

data class VehicleContextPayload(
    val commute_type: String,  // TO_WORK, FROM_WORK, OTHER, UNKNOWN
    val vehicle: Vehicle,
    val navigation: Navigation,
    val traffic: Traffic,
    val weather: Weather,
    val source_mode: String // REAL / SIMULATED / MIXED
)
data class Vehicle(
    val motion_state: String, // MOVING/STOPPED/PARKED/UNKNOWN
    val speed_kmh: Double?,
    val wiper_active: Boolean?,
    val parked_confirmed: Boolean
)
data class Navigation(
    val road_name: String?,
    val road_segment_id: String?,
    val eta_seconds: Int?,
    val guidance_active: Boolean
)
data class Traffic(val congestion: String, val verified: Boolean)
data class Weather(val condition: String, val verified: Boolean)

// Contract must be validated against vehicle-context.schema.json before sending.
// Do NOT map STOPPED => parked_confirmed=true. Brake pedal or speed 0 alone is insufficient.
// If Pleos exposes no road name, send null. Do not invent an official field.

interface RoadPodTransport {
    suspend fun sendEvent(eventJson: String)
    suspend fun acknowledge(commandId: String, status: String)
}

interface SafeAudioExecutor {
    // MUST be invoked locally immediately when nav guidance begins, regardless of network state.
    fun onNavigationStartLocalPause()
    fun onNavigationEndCheckAndResume()
    suspend fun playDjText(commandId: String, text: String)
}
