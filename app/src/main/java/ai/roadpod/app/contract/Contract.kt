package ai.roadpod.app.contract

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.encodeToJsonElement
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Data contract v1.0 — mirrors contracts/vehicle-context.schema.json and
 * contracts/broadcast-command.schema.json exactly (nested structure, snake_case).
 * Unknown values must be null / "UNKNOWN", never invented.
 */
const val SCHEMA_VERSION = "1.0"

val ContractJson = Json {
    encodeDefaults = true
    explicitNulls = true      // schema requires the keys even when the value is null
    ignoreUnknownKeys = true  // tolerate additive server fields
}

object EventType {
    const val SESSION_STARTED = "session.started"
    const val CONTEXT_UPDATED = "context.updated"
    const val USER_UTTERANCE = "user.utterance"
    const val USER_ACTION = "user.action"
    const val NAV_GUIDANCE_STARTED = "navigation.guidance_started"
    const val NAV_GUIDANCE_ENDED = "navigation.guidance_ended"
    const val COMMAND_ACK = "command.ack"
    const val VEHICLE_PARKED = "vehicle.parked"
    const val SESSION_ENDED = "session.ended"
}

object CommandType {
    const val DJ_SPEAK = "dj.speak"
    const val MEDIA_PAUSE = "media.pause"
    const val MEDIA_RESUME = "media.resume"
    const val STORY_PREVIEW = "story.preview"
    const val STORY_INCOMING = "story.incoming"
    const val DIARY_READY = "diary.ready"
    const val UI_STATE = "ui.state"
    /** 음악 코너 (C): play a royalty-free track the server picked, ACK COMPLETED when it ends. */
    const val MUSIC_PLAY = "music.play"
}

@Serializable
data class EventEnvelope(
    @SerialName("schema_version") val schemaVersion: String = SCHEMA_VERSION,
    @SerialName("event_id") val eventId: String,
    @SerialName("event_type") val eventType: String,
    @SerialName("session_id") val sessionId: String,
    val timestamp: String,
    val payload: JsonElement,
) {
    companion object {
        fun create(
            sessionId: String,
            eventType: String,
            payload: JsonElement,
            now: OffsetDateTime = OffsetDateTime.now(),
            eventId: String = UUID.randomUUID().toString(),
        ) = EventEnvelope(
            eventId = eventId,
            eventType = eventType,
            sessionId = sessionId,
            timestamp = now.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME),
            payload = payload,
        )
    }
}

// ---------- context.updated payload (vehicle-context.schema.json) ----------

enum class CommuteType { TO_WORK, FROM_WORK, OTHER, UNKNOWN }
enum class MotionState { MOVING, STOPPED, PARKED, UNKNOWN }
enum class Congestion { FREE, SLOW, JAM, UNKNOWN }
enum class WeatherCondition { CLEAR, CLOUDY, RAIN, SNOW, UNKNOWN }
enum class SourceMode { SIMULATED, REAL, MIXED }

@Serializable
data class VehicleBlock(
    @SerialName("motion_state") val motionState: MotionState,
    @SerialName("speed_kmh") val speedKmh: Double?,
    @SerialName("wiper_active") val wiperActive: Boolean?,
    @SerialName("parked_confirmed") val parkedConfirmed: Boolean,
)

@Serializable
data class NavigationBlock(
    @SerialName("road_name") val roadName: String?,
    @SerialName("road_segment_id") val roadSegmentId: String?,
    @SerialName("eta_seconds") val etaSeconds: Int?,
    @SerialName("guidance_active") val guidanceActive: Boolean,
)

@Serializable
data class TrafficBlock(val congestion: Congestion, val verified: Boolean)

@Serializable
data class WeatherBlock(val condition: WeatherCondition, val verified: Boolean)

@Serializable
data class VehicleContextPayload(
    @SerialName("commute_type") val commuteType: CommuteType,
    val vehicle: VehicleBlock,
    val navigation: NavigationBlock,
    val traffic: TrafficBlock,
    val weather: WeatherBlock,
    @SerialName("source_mode") val sourceMode: SourceMode,
)

// ---------- other app -> server payloads ----------

/** Shape follows mock_client.py (only example in the contract). */
@Serializable
data class UserUtterancePayload(
    val text: String,
    @SerialName("is_final") val isFinal: Boolean,
    @SerialName("input_mode") val inputMode: String = "STT",
    @SerialName("share_mode_enabled") val shareModeEnabled: Boolean,
    /** Server creates a story draft (→ story.preview) only when this is "CREATE_STORY"; null = normal talk. */
    val intent: String? = null,
)

object UtteranceIntent {
    const val CREATE_STORY = "CREATE_STORY"
}

/** user.action payload. The server reads `action`; other keys (e.g. topic_key) are optional per action. */
@Serializable
data class UserActionPayload(
    val action: String,
    /** Only for select_topic. */
    @SerialName("topic_key") val topicKey: String? = null,
)

object UserActionType {
    const val SKIP_TOPIC = "skip_topic"
    const val NEXT_SEGMENT = "next_segment"
    const val RANDOM_TOPIC = "random_topic"
    const val REQUEST_STORY = "request_story"
    /** Server airs a thread of stories drivers passed between each other. */
    const val PLAY_RELAY = "play_relay"
    const val SELECT_TOPIC = "select_topic"
}

@Serializable
data class MusicPlayPayload(
    @SerialName("track_id") val trackId: String,
    val title: String,
    val artist: String? = null,
    val url: String,
    @SerialName("duration_seconds") val durationSeconds: Int,
    val license: String? = null,
)

/** diary.ready: the car shows view_url as a QR code; the driver opens it on a phone. */
@Serializable
data class DiaryReadyPayload(
    @SerialName("diary_id") val diaryId: String,
    @SerialName("view_url") val viewUrl: String,
    /** The diary illustration, shown above the QR on the car screen (optional). */
    @SerialName("image_url") val imageUrl: String? = null,
)

enum class AckStatus { RECEIVED, STARTED, COMPLETED, FAILED, INTERRUPTED }

@Serializable
data class CommandAckPayload(
    @SerialName("command_id") val commandId: String,
    val status: AckStatus,
)

// ---------- server -> app commands (broadcast-command.schema.json) ----------

@Serializable
data class CommandEnvelope(
    @SerialName("schema_version") val schemaVersion: String,
    @SerialName("command_id") val commandId: String,
    @SerialName("session_id") val sessionId: String,
    val type: String,
    @SerialName("issued_at") val issuedAt: String,
    @SerialName("expires_at") val expiresAt: String,
    val payload: JsonObject,
)

enum class VoiceStyle { WARM, BRIGHT, CALM }
enum class InterruptPolicy { DEFER_IF_NAV, DROP_IF_NAV }

@Serializable
data class DjSpeakPayload(
    val text: String,
    @SerialName("voice_style") val voiceStyle: VoiceStyle,
    @SerialName("interrupt_policy") val interruptPolicy: InterruptPolicy,
    /** 큐큐 voice made by the server (OpenAI/Fish TTS) or bundled (asset:...). Null → the car's own TTS. */
    @SerialName("audio_url") val audioUrl: String? = null,
    /** The line asks the driver something ("말씀해 주세요", "보낼까요?"): open the mic right after it ends. */
    @SerialName("listen_after") val listenAfter: Boolean = false,
)

@Serializable
data class StoryPreviewPayload(
    @SerialName("story_id") val storyId: String,
    @SerialName("preview_text") val previewText: String,
    @SerialName("requires_explicit_approval") val requiresExplicitApproval: Boolean,
)

@Serializable
data class StoryIncomingPayload(
    @SerialName("story_id") val storyId: String,
    val text: String,
    @SerialName("audio_url") val audioUrl: String? = null,
)

enum class UiState { IDLE, LISTENING, THINKING, SPEAKING, NAV_SUSPENDED, PARKED }

@Serializable
data class UiStatePayload(val state: UiState)

// ---------- REST ----------

@Serializable
data class SessionStartRequest(
    @SerialName("client_id") val clientId: String,
    @SerialName("commute_type") val commuteType: CommuteType,
)

@Serializable
data class SessionCreated(
    @SerialName("schema_version") val schemaVersion: String,
    @SerialName("session_id") val sessionId: String,
    @SerialName("ws_path") val wsPath: String,
)

@Serializable
data class StoryApprovalRequest(
    @SerialName("session_id") val sessionId: String,
    val approved: Boolean,
)

inline fun <reified T> T.toJsonElement(): JsonElement = ContractJson.encodeToJsonElement(this)
