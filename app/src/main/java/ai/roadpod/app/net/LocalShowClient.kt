package ai.roadpod.app.net

import ai.roadpod.app.contract.CommuteType
import ai.roadpod.app.contract.EventEnvelope
import ai.roadpod.app.contract.EventType
import ai.roadpod.app.contract.SCHEMA_VERSION
import ai.roadpod.app.contract.CommandEnvelope
import ai.roadpod.app.contract.SessionCreated
import android.os.Handler
import android.os.Looper
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import java.time.LocalTime
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * 체험(목업) build: the broadcast server runs inside the app, on a prepared script.
 *
 * Everything else is the real app: Pleos vehicle data and navigation, nav-first pausing, voice input,
 * parking detection and the car screen all run as in the service build. Only the show itself comes from
 * here instead of the server, so the APK needs no network and no API key.
 *
 * It answers the same events the real server does, with the same commands:
 *  - session.started → today's topic (morning or evening by the clock), a deep dive, a listener story and,
 *    a little later, a reply to it with 큐큐's own comment
 *  - wiper on → a short driving comment; stopped for a while → an invitation to send a story
 *  - request_story / an utterance → empathy, a story draft read back, "보낼까요?" (mic opens by itself)
 *  - "이 노래 드랍해줘" → a note left on this stretch
 *  - navigation guidance → the cut line is resumed when guidance ends
 *  - vehicle.parked → closing line and Q Diary (illustration + QR)
 */
class LocalShowClient : RoadpodClient("http://local.myq", "") {
    private val main = Handler(Looper.getMainLooper())
    private var listener: RoadpodClient.Listener? = null
    private val sid = "local-" + UUID.randomUUID().toString().take(8)

    /** One step of the show: a line (with its bundled voice), a story, or a pause. */
    private data class Step(
        val id: String = "", val story: Boolean = false, val listenAfter: Boolean = false,
        val waitMs: Long = 0, val text: String? = null,
    )

    private data class Topic(val key: String, val question: String, val category: String, val label: String, val steps: List<Step>)

    private val topicA = Topic("rude", "무례한 사람을 내 편으로 만들 수 있을까?", "RELATION", "관계와 소통", listOf(
        Step("q_A1"), Step("q_A2"), Step("q_A4a", story = true), Step("q_A4b"),
        Step(waitMs = 15_000), Step("q_A4c", story = true), Step("q_A4d"),
    ))
    private val topicB = Topic("workoff", "나는 왜 퇴근하고도 회사 생각을 멈추지 못할까?", "WORK", "일과 성장", listOf(
        Step("q_B1"), Step("q_B4a", story = true), Step("q_B4b"), Step(waitMs = 15_000), Step("q_B4c", story = true),
    ))
    private val topicC = Topic("rest", "쉬어도 쉰 것 같지 않은 이유는 뭘까?", "MIND", "마음과 심리", listOf(Step("q_C1")))
    private val evening = LocalTime.now().hour >= 15
    private val order = if (evening) listOf(topicB, topicA, topicC) else listOf(topicA, topicB, topicC)
    private var topicIdx = 0
    private var progress = 0

    private val queue = ArrayDeque<Step>()
    private var playing: String? = null           // command_id we wait for
    private var playingStep: Step? = null
    private var interrupted: Step? = null         // cut by navigation guidance, said again afterwards
    private var nav = false
    private var waiting = false
    private var storyStage = 0                     // 0 none, 1 listening for a story, 2 "보낼까요?" asked
    private var rainDone = false
    private var stoppedPromptDone = false
    private var stoppedSince = 0L
    private var parkedDone = false

    // ---------------- RoadpodClient ----------------

    override suspend fun health() = true
    override suspend fun startSession(clientId: String, commuteType: CommuteType) = SessionCreated(SCHEMA_VERSION, sid, "/local")
    override suspend fun now(sessionId: String): String = buildJsonObject {
        val t = order[topicIdx % order.size]
        put("commute_type", if (evening) "FROM_WORK" else "TO_WORK")
        put("content_ready", true)
        putJsonObject("topic") { put("key", t.key); put("question", t.question); put("category", t.category); put("category_label", t.label) }
        putJsonObject("progress") { put("done", progress.coerceAtLeast(1)); put("total", t.steps.count { it.id.isNotEmpty() }); put("role", if (progress <= 1) "HOOK" else "PERSPECTIVE") }
    }.toString()
    override suspend fun topics(): String = buildJsonObject {
        putJsonObject("categories") { put("RELATION", "관계와 소통"); put("WORK", "일과 성장"); put("MIND", "마음과 심리") }
        put("topics", kotlinx.serialization.json.JsonArray(order.map { t ->
            buildJsonObject { put("key", t.key); put("category", t.category); put("question", t.question) }
        }))
    }.toString()
    override suspend fun sendStoryApproval(storyId: String, sessionId: String, approved: Boolean) = 200

    override fun connect(wsPath: String, listener: RoadpodClient.Listener) {
        this.listener = listener
        main.post { listener.onOpen() }
        main.postDelayed(ticker, 1000)
    }

    override fun send(event: EventEnvelope): Boolean { main.post { onEvent(event) }; return true }

    override fun close() { listener = null; main.removeCallbacksAndMessages(null) }

    // ---------------- the show ----------------

    private fun startTopic(i: Int) {
        topicIdx = i; progress = 0
        queue.clear(); queue.addAll(order[i % order.size].steps)
        pump()
    }

    private val ticker = object : Runnable {
        override fun run() {
            // stopped for a few seconds, nothing else going on → invite a story (once)
            if (!stoppedPromptDone && stoppedSince > 0 && System.currentTimeMillis() - stoppedSince > 4000 && storyStage == 0 && !nav && !parkedDone) {
                stoppedPromptDone = true
                queue.addFirst(Step("m_stopped"))
                pump()
            }
            main.postDelayed(this, 1000)
        }
    }

    private fun onEvent(e: EventEnvelope) {
        val p = e.payload as? JsonObject ?: JsonObject(emptyMap())
        when (e.eventType) {
            EventType.SESSION_STARTED -> startTopic(0)
            EventType.COMMAND_ACK -> {
                val id = p["command_id"]?.jsonPrimitive?.contentOrNull
                val st = p["status"]?.jsonPrimitive?.contentOrNull
                if (id != playing || st == "RECEIVED" || st == "STARTED") return
                if (st == "INTERRUPTED" && nav) interrupted = playingStep
                val asked = st == "COMPLETED" && playingStep?.listenAfter == true
                playing = null; playingStep = null
                if (asked) {   // a question: hold the show until the driver answers (or 10 s pass)
                    waiting = true
                    main.postDelayed(answerTimeout, 10_000)
                    return
                }
                main.postDelayed({ pump() }, 300)
            }
            EventType.NAV_GUIDANCE_STARTED -> nav = true
            EventType.NAV_GUIDANCE_ENDED -> {
                nav = false
                interrupted?.let { queue.addFirst(it); queue.addFirst(Step("m_resume")) }
                interrupted = null
                pump()
            }
            EventType.CONTEXT_UPDATED -> {
                val v = p["vehicle"]?.jsonObject
                val wiper = v?.get("wiper_active")?.jsonPrimitive?.booleanOrNull == true
                val speed = v?.get("speed_kmh")?.jsonPrimitive?.doubleOrNull
                if (wiper && !rainDone) { rainDone = true; queue.addFirst(Step("m_rain")); pump() }
                stoppedSince = if (speed != null && speed < 0.5) (if (stoppedSince == 0L) System.currentTimeMillis() else stoppedSince) else 0L
            }
            EventType.USER_ACTION -> when (p["action"]?.jsonPrimitive?.contentOrNull) {
                "request_story" -> { storyStage = 1; queue.addFirst(Step("m_listen", listenAfter = true)); pump() }
                "skip_topic", "random_topic" -> { interruptCurrent(); startTopic(topicIdx + 1) }
                "select_topic" -> {
                    val key = p["topic_key"]?.jsonPrimitive?.contentOrNull
                    val i = order.indexOfFirst { it.key == key }
                    if (i >= 0) { interruptCurrent(); startTopic(i) }
                }
                "play_relay" -> { queue.addFirst(Step("q_A4d")); queue.addFirst(Step("q_A4c", story = true)); pump() }
            }
            EventType.USER_UTTERANCE -> onUtterance(p["text"]?.jsonPrimitive?.contentOrNull.orEmpty(), p["intent"]?.jsonPrimitive?.contentOrNull)
            EventType.VEHICLE_PARKED -> if (!parkedDone) {
                parkedDone = true
                queue.clear(); queue.addLast(Step("q_B8")); queue.addLast(Step(id = "diary"))
                pump()
            }
        }
    }

    private val answerTimeout = Runnable { waiting = false; pump() }

    private fun onUtterance(text: String, intent: String?) {
        main.removeCallbacks(answerTimeout); waiting = false
        val t = text.replace(" ", "").trimEnd('.', '!', '?', '~')
        // short answers only: "응", "네", "보내", "응 보내줘" — not any sentence that happens to end in 네요
        val yes = t.length <= 12 && (t.contains("보내") && !t.contains("보내지") && !t.contains("안보내") ||
            Regex("^(응|네|예|어|좋아|그래|오케이|ㅇㅇ)").containsMatchIn(t))
        val no = t.length <= 14 && Regex("아니|싫|취소|보내지마|안보내|됐어").containsMatchIn(t)
        when {
            storyStage == 2 && no -> { storyStage = 0; queue.addFirst(Step("m_keep")) }
            storyStage == 2 && yes -> { storyStage = 0; queue.addFirst(Step("q_A9")) }
            t.contains("드랍") || t.contains("드롭") -> queue.addFirst(Step("q_B5"))
            storyStage == 1 || intent == "CREATE_STORY" || t.contains("사연") -> {
                storyStage = 2
                // a beat, empathy, the draft read back, then "보낼까요?" with the mic opening by itself
                queue.addFirst(Step("q_A8", listenAfter = true)); queue.addFirst(Step("q_A7")); queue.addFirst(Step("q_A7pre")); queue.addFirst(Step(waitMs = 1200))
            }
            else -> queue.addFirst(Step("m_thanks"))
        }
        pump()
    }

    private fun interruptCurrent() { playing = null; playingStep = null }

    private fun pump() {
        if (playing != null || nav || waiting || listener == null) return
        val step = queue.removeFirstOrNull() ?: return
        if (step.waitMs > 0) { waiting = true; main.postDelayed({ waiting = false; pump() }, step.waitMs); return }
        if (step.id == "diary") {
            emit("diary.ready", buildJsonObject {
                put("diary_id", "local-diary"); put("view_url", "https://myq.example/diary/demo"); put("image_url", "mock/diary.jpg")
            })
            main.postDelayed({ pump() }, 300)
            return
        }
        if (step in order[topicIdx % order.size].steps) progress++   // the topic's own lines only
        val text = step.text ?: LocalShowLines.text[step.id] ?: return pump()
        val payload = if (step.story) buildJsonObject {
            put("story_id", "local-${step.id}"); put("text", text); put("audio_url", "asset:headunit/mock/${step.id}.mp3")
        } else buildJsonObject {
            put("text", text); put("voice_style", if (evening) "CALM" else "BRIGHT"); put("interrupt_policy", "DEFER_IF_NAV")
            put("audio_url", "asset:headunit/mock/${step.id}.mp3")
            if (step.listenAfter) put("listen_after", true)
        }
        playingStep = step
        playing = emit(if (step.story) "story.incoming" else "dj.speak", payload)
    }

    private fun emit(type: String, payload: JsonObject): String {
        val now = OffsetDateTime.now()
        val c = CommandEnvelope(
            schemaVersion = SCHEMA_VERSION, commandId = "local-" + UUID.randomUUID(), sessionId = sid, type = type,
            issuedAt = now.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME),
            expiresAt = now.plusMinutes(10).format(DateTimeFormatter.ISO_OFFSET_DATE_TIME),
            payload = payload,
        )
        listener?.onCommand(c)
        return c.commandId
    }

}
