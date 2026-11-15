package ai.roadpod.app.net

import ai.roadpod.app.contract.CommandEnvelope
import ai.roadpod.app.contract.CommuteType
import ai.roadpod.app.contract.ContractJson
import ai.roadpod.app.contract.EventEnvelope
import ai.roadpod.app.contract.SessionCreated
import ai.roadpod.app.contract.SessionStartRequest
import ai.roadpod.app.contract.StoryApprovalRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.jsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * REST + WebSocket client for ROADPOD API v1. Open: the 체험 build swaps in [LocalShowClient] (the show runs in the app).
 * - Token goes in the Authorization header only (never in the URL).
 * - Unparseable server messages (e.g. {"error": ...}) are surfaced via [Listener.onServerError].
 */
open class RoadpodClient(
    private val baseUrl: String,          // e.g. http://10.0.2.2:8765
    private val token: String,
    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)   // WebSocket: no read timeout
        .pingInterval(20, TimeUnit.SECONDS)
        .build(),
) {
    interface Listener {
        fun onOpen()
        fun onCommand(command: CommandEnvelope)
        fun onServerError(raw: String)
        fun onClosed(code: Int, reason: String)
        fun onFailure(t: Throwable)
    }

    private val jsonType = "application/json; charset=utf-8".toMediaType()
    private var socket: WebSocket? = null

    /** Car screen: what's on air now (question, category, progress). Raw JSON, or null on any failure. */
    open suspend fun now(sessionId: String): String? = getJson("$baseUrl/api/v1/sessions/$sessionId/now")

    /** Car screen: topic list for the 주제 sheet. Raw JSON, or null on any failure. */
    open suspend fun topics(): String? = getJson("$baseUrl/api/v1/topics")

    private suspend fun getJson(url: String): String? = withContext(Dispatchers.IO) {
        val req = Request.Builder().url(url).header("Authorization", "Bearer $token").get().build()
        runCatching { http.newCall(req).execute().use { if (it.isSuccessful) it.body?.string() else null } }.getOrNull()
    }

    open suspend fun health(): Boolean = withContext(Dispatchers.IO) {
        val req = Request.Builder().url("$baseUrl/api/v1/health").get().build()
        runCatching { http.newCall(req).execute().use { it.isSuccessful } }.getOrDefault(false)
    }

    open suspend fun startSession(clientId: String, commuteType: CommuteType): SessionCreated =
        withContext(Dispatchers.IO) {
            val body = ContractJson.encodeToString(SessionStartRequest.serializer(), SessionStartRequest(clientId, commuteType))
            val req = Request.Builder()
                .url("$baseUrl/api/v1/sessions")
                .header("Authorization", "Bearer $token")
                .post(body.toRequestBody(jsonType))
                .build()
            http.newCall(req).execute().use { resp ->
                if (resp.code != 201) throw IOException("startSession HTTP ${resp.code}")
                ContractJson.decodeFromString(SessionCreated.serializer(), resp.body!!.string())
            }
        }

    /** Future endpoint in the contract (mock server does not implement it yet). */
    open suspend fun sendStoryApproval(storyId: String, sessionId: String, approved: Boolean): Int =
        withContext(Dispatchers.IO) {
            val body = ContractJson.encodeToString(StoryApprovalRequest.serializer(), StoryApprovalRequest(sessionId, approved))
            val req = Request.Builder()
                .url("$baseUrl/api/v1/stories/$storyId/approval")
                .header("Authorization", "Bearer $token")
                .post(body.toRequestBody(jsonType))
                .build()
            http.newCall(req).execute().use { it.code }
        }

    open fun connect(wsPath: String, listener: Listener) {
        val wsUrl = baseUrl.replaceFirst("http://", "ws://").replaceFirst("https://", "wss://") + wsPath
        val req = Request.Builder().url(wsUrl).header("Authorization", "Bearer $token").build()
        socket = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) = listener.onOpen()

            override fun onMessage(webSocket: WebSocket, text: String) {
                val parsed = runCatching {
                    val obj = ContractJson.parseToJsonElement(text).jsonObject
                    if ("command_id" in obj) ContractJson.decodeFromJsonElement(CommandEnvelope.serializer(), obj) else null
                }.getOrNull()
                if (parsed != null) listener.onCommand(parsed) else listener.onServerError(text)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = listener.onClosed(code, reason)
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = listener.onFailure(t)
        })
    }

    open fun send(event: EventEnvelope): Boolean {
        val s = socket ?: return false
        return s.send(ContractJson.encodeToString(EventEnvelope.serializer(), event))
    }

    open fun close() {
        socket?.close(1000, "session ended")
        socket = null
    }
}
