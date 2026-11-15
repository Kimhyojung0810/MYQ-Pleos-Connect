package ai.roadpod.app

import ai.roadpod.app.command.CommandGate
import ai.roadpod.app.contract.AckStatus
import ai.roadpod.app.contract.CommandAckPayload
import ai.roadpod.app.contract.CommandEnvelope
import ai.roadpod.app.contract.CommandType
import ai.roadpod.app.contract.CommuteType
import ai.roadpod.app.contract.ContractJson
import ai.roadpod.app.contract.DiaryReadyPayload
import ai.roadpod.app.contract.MusicPlayPayload
import ai.roadpod.app.contract.DjSpeakPayload
import ai.roadpod.app.contract.EventEnvelope
import ai.roadpod.app.contract.EventType
import ai.roadpod.app.contract.StoryIncomingPayload
import ai.roadpod.app.contract.StoryPreviewPayload
import ai.roadpod.app.contract.UiState
import ai.roadpod.app.contract.UiStatePayload
import ai.roadpod.app.contract.UserActionPayload
import ai.roadpod.app.contract.UserActionType
import ai.roadpod.app.contract.UserUtterancePayload
import ai.roadpod.app.contract.UtteranceIntent
import ai.roadpod.app.contract.VehicleContextPayload
import ai.roadpod.app.contract.toJsonElement
import ai.roadpod.app.logic.AttentionEvaluator
import ai.roadpod.app.logic.ContextBuilder
import ai.roadpod.app.logic.ParkedAnnouncer
import ai.roadpod.app.logic.ParkingDetector
import ai.roadpod.app.net.RoadpodClient
import ai.roadpod.app.speech.DjSpeaker
import ai.roadpod.app.speech.MusicPlayer
import ai.roadpod.app.speech.SpeechInput
import ai.roadpod.app.vehicle.CarPropertySource
import ai.roadpod.app.vehicle.NaviSource
import ai.roadpod.app.vehicle.PleosVehicleSource
import ai.roadpod.app.vehicle.SignalHub
import ai.roadpod.app.vehicle.VehicleSignals
import android.content.Context
import android.os.Build
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import java.time.OffsetDateTime

/**
 * App-side orchestration for M1~M3:
 *  vehicle sources → SignalHub → (ParkingDetector, AttentionEvaluator, ContextBuilder) → context.updated
 *  server commands → CommandGate → DjSpeaker / UI → command.ack
 *  SpeechInput (final only) → user.utterance
 */
class RoadpodController(
    private val appContext: Context,
    private val baseUrl: String,
    private val token: String,
) {
    data class StoryApprovalRequest(val commandId: String, val storyId: String, val previewText: String)

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    val hub = SignalHub()

    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log
    private val _uiState = MutableStateFlow(UiState.IDLE)
    val uiState: StateFlow<UiState> = _uiState
    private val _attention = MutableStateFlow<AttentionEvaluator.Result?>(null)
    val attention: StateFlow<AttentionEvaluator.Result?> = _attention
    private val _parking = MutableStateFlow<ParkingDetector.State>(ParkingDetector.State.NotArrived)
    val parking: StateFlow<ParkingDetector.State> = _parking
    /** story.preview waiting for the driver's decision; shown only when parked. */
    private val _approval = MutableStateFlow<StoryApprovalRequest?>(null)
    val approval: StateFlow<StoryApprovalRequest?> = _approval
    /** story.preview requests held until parking (the car screen shows only the count while driving). */
    private val _heldStories = MutableStateFlow(0)
    val heldStories: StateFlow<Int> = _heldStories
    /** The next final utterance will be sent as a story (after [requestStory]); shown on the car screen. */
    private val _storyMode = MutableStateFlow(false)
    val storyMode: StateFlow<Boolean> = _storyMode
    /** 오늘의 도로일기 waiting to be shown as a QR code (null = nothing to show). */
    private val _diary = MutableStateFlow<DiaryReadyPayload?>(null)
    val diary: StateFlow<DiaryReadyPayload?> = _diary
    fun dismissDiary() { _diary.value = null }

    /** What the car screen (assets/headunit) shows next to the state above. */
    data class Caption(val who: String, val text: String, val kind: String)   // kind: dj | story | me | music | soft
    private val _caption = MutableStateFlow(Caption("큐큐", "서버에 연결하고 있어요.", "soft"))
    val caption: StateFlow<Caption> = _caption
    private val _conn = MutableStateFlow("connecting")          // connecting | online | offline
    val conn: StateFlow<String> = _conn
    private val _onAir = MutableStateFlow<String?>(null)        // raw JSON of GET /api/v1/sessions/{id}/now
    val onAir: StateFlow<String?> = _onAir
    private val _topics = MutableStateFlow<String?>(null)       // raw JSON of GET /api/v1/topics
    val topics: StateFlow<String?> = _topics
    private val _music = MutableStateFlow<String?>(null)        // title while a music.play track is playing
    val music: StateFlow<String?> = _music

    /** 체험 build (BuildConfig.MOCK): the show comes from an in-app script instead of the server; everything else is real. */
    private val client: RoadpodClient = if (BuildConfig.MOCK) ai.roadpod.app.net.LocalShowClient() else RoadpodClient(baseUrl, token)
    private val gate = CommandGate()
    // accel missing → gear P fallback. The 체험 build confirms parking after 20 s instead of 5 min,
    // so the closing and Q Diary can be seen without a long wait; the service build keeps 5 min.
    private val parkingDetector = if (BuildConfig.MOCK) ParkingDetector(quietPeriodMs = 20_000L) else ParkingDetector()
    private val attentionEvaluator = AttentionEvaluator()
    private val contextBuilder = ContextBuilder()
    private val throttle = ContextBuilder.SendThrottle()
    private val speaker = DjSpeaker(appContext, ::status)
    private val speechInput = SpeechInput(appContext, ::onFinalUtterance, ::onSttStatus)
    private val musicPlayer = MusicPlayer(::status)
    private var musicCmd: CommandEnvelope? = null

    private val navi = NaviSource(appContext, hub, onArrived = { status("arrived → parking monitor") }, onStatus = ::status)
    private val pleosVehicle = PleosVehicleSource(appContext, hub, onActivity = {}, onStatus = ::status)
    private val carProps = CarPropertySource(appContext, hub, onActivity = {}, onStatus = ::status)

    private var sessionId: String? = null
    private var navGuidanceActive = false
    private var mediaPaused = false
    var shareModeEnabled = false
    private val speakQueue = ArrayDeque<Line>()
    /** One spoken line: its command, text, and the server-made voice if there is one. */
    private data class Line(val cmd: CommandEnvelope, val text: String, val audioUrl: String?, val listenAfter: Boolean = false)
    private val deferred = ArrayDeque<CommandEnvelope>()
    private val heldPreviews = ArrayDeque<StoryApprovalRequest>()
    private val parkedAnnouncer = ParkedAnnouncer()
    private var tickJob: Job? = null
    private val onEmulator = Build.FINGERPRINT.contains("generic") || Build.HARDWARE.contains("ranchu") ||
        Build.PRODUCT.contains("sdk") || Build.MODEL.contains("emulator", ignoreCase = true)

    // ---------------- lifecycle ----------------

    private var running = false

    /** Idempotent: owned by [RoadpodService] so it keeps running while another app (navigation) is in front. */
    fun start(clientId: String) {
        if (running) return
        running = true
        navi.start(); pleosVehicle.start()
        // Car.createCar + property configs took ~6 s on the emulator main thread → ANR kill. Run off main.
        scope.launch(Dispatchers.IO) { carProps.start() }
        speaker.start(); speechInput.start()
        scope.launch {
            if (!client.health()) { status("server.health_failed ($baseUrl)"); return@launch }
            val created = runCatching { client.startSession(clientId, CommuteType.UNKNOWN) }
                .getOrElse { status("session.start_failed: ${it.message}"); return@launch }
            sessionId = created.sessionId
            wsPath = created.wsPath
            status("session ${created.sessionId.take(8)} created")
            loadTopics()
            client.connect(created.wsPath, socketListener)
        }
        tickJob = scope.launch { while (isActive) { tick(); delay(1000) } }
    }

    fun stop() {
        if (!running) return
        running = false
        tickJob?.cancel()
        sessionId?.let { sendEvent(EventType.SESSION_ENDED, JsonObject(emptyMap())) }
        client.close()
        navi.stop(); pleosVehicle.stop(); carProps.stop()
        speaker.release(); speechInput.release(); musicPlayer.stop()
        reconnectJob?.cancel()
        scope.cancel()          // instance is discarded by RoadpodApp.releaseController()
        status("session.ended")
    }

    // ---------------- inputs ----------------

    /** Dev trigger (wake phrase / Gleo trigger undecided). */
    fun listenOnce() = speechInput.listenOnce()

    private fun onSttStatus(msg: String) {
        status(msg)
        post {
            if (msg == "stt.listening") {
                _uiState.value = UiState.LISTENING
            } else if ((msg == "stt.ended" || msg.startsWith("stt.error")) && _uiState.value == UiState.LISTENING) {
                _uiState.value = UiState.IDLE
                drainSpeech()   // lines held while the mic was open
            }
        }
    }

    /** 주제 sheet on the car screen (stopped only, enforced by the caller). */
    fun selectTopic(key: String) {
        status("user.action select_topic $key")
        sendEvent(EventType.USER_ACTION, UserActionPayload(UserActionType.SELECT_TOPIC, key).toJsonElement())
    }

    private var onAirJob: Job? = null
    private fun refreshOnAir() {
        val sid = sessionId ?: return
        if (onAirJob?.isActive == true) return
        onAirJob = scope.launch { client.now(sid)?.let { _onAir.value = it } }
    }

    private fun loadTopics() {
        scope.launch { client.topics()?.let { _topics.value = it } }
    }

    /** Also used by the debug receiver to inject text as if it were a final STT result. */
    fun onFinalUtterance(text: String) {
        val asStory = shareModeEnabled || _storyMode.value
        _storyMode.value = false
        status("utterance(final) len=${text.length} story=$asStory")
        _caption.value = Caption("나", text, "me")
        sendEvent(
            EventType.USER_UTTERANCE,
            UserUtterancePayload(
                text = text, isFinal = true, shareModeEnabled = asStory,
                intent = if (asStory) UtteranceIntent.CREATE_STORY else null,
            ).toJsonElement(),
        )
    }

    /** user.action — e.g. skip_topic, next_segment. Callers enforce the "stopped only" driving rule. */
    fun sendAction(action: String) {
        status("user.action $action")
        sendEvent(EventType.USER_ACTION, UserActionPayload(action).toJsonElement())
    }

    /** Tells the server a story is coming (it asks the driver to speak) and marks the next utterance as the story. */
    fun requestStory() {
        _storyMode.value = true
        sendAction(UserActionType.REQUEST_STORY)
    }

    /** Navigation guidance on/off. Real source undecided (see guide 3-1); currently driven by the debug receiver. */
    fun setNavGuidance(active: Boolean) {
        if (active == navGuidanceActive) return
        navGuidanceActive = active
        sendEvent(if (active) EventType.NAV_GUIDANCE_STARTED else EventType.NAV_GUIDANCE_ENDED, JsonObject(emptyMap()))
        if (active) {
            speaker.stopNow()                 // local priority: silence immediately
            stopMusic()
            _uiState.value = UiState.NAV_SUSPENDED
        } else {
            _uiState.value = UiState.IDLE
            val now = OffsetDateTime.now()
            while (deferred.isNotEmpty()) {
                val c = deferred.removeFirst()
                if (gate.stillValid(c, now)) enqueueSpeak(c) else ack(c.commandId, AckStatus.FAILED)
            }
            drainSpeech()
        }
    }

    fun answerStory(approved: Boolean) {
        val req = _approval.value ?: return
        val sid = sessionId ?: return
        _approval.value = null
        scope.launch {
            val code = runCatching { client.sendStoryApproval(req.storyId, sid, approved) }.getOrElse { -1 }
            status("story.approval approved=$approved http=$code")
            ack(req.commandId, if (code in 200..299) AckStatus.COMPLETED else AckStatus.FAILED)
            showNextHeldPreview()
        }
    }

    // ---------------- socket ----------------

    private val socketListener = object : RoadpodClient.Listener {
        override fun onOpen() = post {
            status("ws.open")
            _conn.value = "online"
            refreshOnAir()
            if (reconnectAttempt == 0) sendEvent(EventType.SESSION_STARTED, JsonObject(emptyMap()))
            reconnectAttempt = 0
            throttle.forceNext()          // resend current context after (re)connect
            parkedAnnouncer.resend()      // and vehicle.parked, in case the drop swallowed it
        }
        override fun onCommand(command: CommandEnvelope) = post { handleCommand(command) }
        override fun onServerError(raw: String) = post { status("server.error ${raw.take(120)}") }
        override fun onClosed(code: Int, reason: String) = post { status("ws.closed $code"); _conn.value = "offline"; scheduleReconnect() }
        override fun onFailure(t: Throwable) = post {
            status("ws.failure ${t.javaClass.simpleName}: ${t.message}")
            _conn.value = "offline"
            stopMusic()
            // Local safety stays active; queued speech from before the drop is discarded (no stale replay).
            speakQueue.clear(); deferred.clear()
            scheduleReconnect()
        }
    }

    private var wsPath: String? = null
    private var reconnectAttempt = 0
    private var reconnectJob: Job? = null

    /** Reconnect to the same session with backoff (1,2,4,8,16,30s). Stale commands are already filtered by expires_at. */
    private fun scheduleReconnect() {
        val path = wsPath ?: return
        if (reconnectJob?.isActive == true || tickJob?.isActive != true) return
        val waitMs = (1000L shl reconnectAttempt.coerceAtMost(4)).coerceAtMost(30_000L)
        reconnectAttempt++
        reconnectJob = scope.launch {
            delay(waitMs)
            status("ws.reconnect attempt=$reconnectAttempt")
            client.connect(path, socketListener)
        }
    }

    private fun handleCommand(c: CommandEnvelope) {
        val sid = sessionId ?: return
        status("cmd.recv ${c.type} ${c.commandId.take(8)}")
        when (val d = gate.evaluate(c, sid, OffsetDateTime.now(), navGuidanceActive)) {
            is CommandGate.Decision.Duplicate -> status("cmd.duplicate ${d.commandId.take(8)}")
            is CommandGate.Decision.Drop -> {
                status("cmd.drop ${c.type} ${d.reason}")
                ack(c.commandId, if (d.reason == "NAV_ACTIVE") AckStatus.INTERRUPTED else AckStatus.FAILED)
            }
            is CommandGate.Decision.Defer -> { ack(c.commandId, AckStatus.RECEIVED); deferred.addLast(c); status("cmd.defer ${c.type}") }
            is CommandGate.Decision.Execute -> { ack(c.commandId, AckStatus.RECEIVED); execute(c) }
        }
    }

    private fun execute(c: CommandEnvelope) {
        when (c.type) {
            CommandType.DJ_SPEAK -> enqueueSpeak(c)
            CommandType.STORY_INCOMING -> {
                val p = decode(c, StoryIncomingPayload.serializer()) ?: return
                speakQueue.addLast(Line(c, p.text, p.audioUrl)); drainSpeech()
            }
            CommandType.MEDIA_PAUSE -> {
                mediaPaused = true; speaker.stopNow(); stopMusic(); ack(c.commandId, AckStatus.COMPLETED)
            }
            CommandType.MEDIA_RESUME -> {
                mediaPaused = false; ack(c.commandId, AckStatus.COMPLETED); drainSpeech()
            }
            CommandType.UI_STATE -> {
                val p = decode(c, UiStatePayload.serializer()) ?: return
                _uiState.value = p.state; ack(c.commandId, AckStatus.COMPLETED)
            }
            CommandType.STORY_PREVIEW -> {
                val p = decode(c, StoryPreviewPayload.serializer()) ?: return
                val req = StoryApprovalRequest(c.commandId, p.storyId, p.previewText)
                // 3번 페이지: 운전에 지장을 줄 수 있으면 승인은 주차 후로 보류
                if (_parking.value is ParkingDetector.State.Parked && _approval.value == null) _approval.value = req
                else { heldPreviews.addLast(req); _heldStories.value = heldPreviews.size; status("story.preview held until parked") }
            }
            CommandType.DIARY_READY -> {
                val p = decode(c, DiaryReadyPayload.serializer()) ?: return
                // Server sends it only after confirmed parking; shown as a QR the driver scans with a phone.
                _diary.value = p
                status("diary.ready ${p.diaryId.takeLast(8)}")
                ack(c.commandId, AckStatus.COMPLETED)
            }
            CommandType.MUSIC_PLAY -> playMusic(c)
            else -> { status("cmd.unknown ${c.type}"); ack(c.commandId, AckStatus.FAILED) }
        }
    }

    // ---------------- 음악 코너 (C) ----------------

    private fun playMusic(c: CommandEnvelope) {
        val p = decode(c, MusicPlayPayload.serializer()) ?: return
        if (navGuidanceActive || mediaPaused || speaker.isSpeaking) { ack(c.commandId, AckStatus.INTERRUPTED); return }
        musicCmd = c
        val label = listOfNotNull(p.title, p.artist?.takeIf { it.isNotBlank() }).joinToString(" - ")
        _music.value = label
        _caption.value = Caption("♪ 음악", label, "music")
        status("music.play ${p.trackId}")
        musicPlayer.play(p.url, object : MusicPlayer.Callback {
            override fun onStarted() = post { if (musicCmd === c) ack(c.commandId, AckStatus.STARTED) }
            override fun onCompleted() = post { musicDone(c, AckStatus.COMPLETED) }
            override fun onFailed(reason: String) = post { status("music.fail $reason"); musicDone(c, AckStatus.FAILED) }
        })
    }

    private fun musicDone(c: CommandEnvelope, st: AckStatus) {
        if (musicCmd !== c) return
        musicCmd = null
        _music.value = null
        ack(c.commandId, st)
        refreshOnAir()
        drainSpeech()
    }

    /** Navigation guidance, media.pause, connection loss: the track stops and the server moves on. */
    private fun stopMusic() {
        val c = musicCmd ?: return
        musicPlayer.stop()
        musicDone(c, AckStatus.INTERRUPTED)
    }

    private fun enqueueSpeak(c: CommandEnvelope) {
        val p = decode(c, DjSpeakPayload.serializer()) ?: return
        speakQueue.addLast(Line(c, p.text, p.audioUrl, p.listenAfter)); drainSpeech()
    }

    /** A short breath between two lines so the show doesn't run on without a pause. */
    private var lineGapJob: Job? = null

    private fun drainSpeech() {
        // Never talk over an open mic (a listen_after question): the driver's answer comes first.
        if (navGuidanceActive || mediaPaused || speaker.isSpeaking || musicPlayer.isPlaying || lineGapJob?.isActive == true ||
            _uiState.value == UiState.LISTENING) return
        val (cmd, text, audioUrl, listenAfter) = speakQueue.removeFirstOrNull() ?: return
        _uiState.value = UiState.SPEAKING
        _caption.value = if (cmd.type == CommandType.STORY_INCOMING) Caption("💌 사연", text, "story") else Caption("큐큐", text, "dj")
        refreshOnAir()
        speaker.speak(text, object : DjSpeaker.Callback {
            override fun onStarted() = post { ack(cmd.commandId, AckStatus.STARTED) }
            override fun onCompleted() = post {
                ack(cmd.commandId, AckStatus.COMPLETED); _uiState.value = UiState.IDLE
                // A question: the driver answers by voice without touching the screen. Hold the show while the mic is open
                // (12 s at most, in case the recognizer never reports back).
                if (listenAfter && !navGuidanceActive && speechInput.listenOnce()) {
                    _uiState.value = UiState.LISTENING
                    scope.launch { delay(12_000); if (_uiState.value == UiState.LISTENING) { _uiState.value = UiState.IDLE; drainSpeech() } }
                }
                // Clear our own job before draining: drainSpeech() returns early while lineGapJob is active.
                lineGapJob = scope.launch { delay(LINE_GAP_MS); lineGapJob = null; drainSpeech() }
            }
            override fun onInterrupted() = post { ack(cmd.commandId, AckStatus.INTERRUPTED); if (!navGuidanceActive) _uiState.value = UiState.IDLE }
            override fun onFailed(reason: String) = post { status("tts.fail $reason"); ack(cmd.commandId, AckStatus.FAILED); _uiState.value = UiState.IDLE; drainSpeech() }
        }, audioUrl)
    }

    // ---------------- periodic ----------------

    private var ticks = 0

    private fun tick() {
        if (++ticks % 5 == 0) refreshOnAir()   // the screen's question/progress stays fresh even between lines
        val s = hub.signals.value
        val now = System.currentTimeMillis()
        val (state, askNow) = parkingDetector.evaluate(
            ParkingDetector.Input(
                nowMs = now, arrivedAtMs = s.destinationArrivedAtMs, accelSupported = s.accelSupported,
                accelPressed = s.accelPedalPercent?.let { it > 0.0 }, brakePressed = s.brakePressed,
                gearPark = s.gearSelection?.let { it == CarPropertySource.GEAR_PARK },
            )
        )
        if (state.javaClass != _parking.value.javaClass) status("parking=${state.javaClass.simpleName}")
        _parking.value = state
        if (askNow) localAsk("주차 완료하셨나요?")
        val parked = state is ParkingDetector.State.Parked
        if (parked) showNextHeldPreview()

        _attention.value = attentionEvaluator.evaluate(s)
        val payload = contextBuilder.build(s, parked, onEmulator)
        // The server ignores vehicle.parked unless its latest context says parked_confirmed=true,
        // so the parked context goes out first (bypassing the throttle), then vehicle.parked.
        val announced = sessionId != null && parkedAnnouncer.update(
            parked,
            sendParkedContext = { sendContext(payload).also { if (it) throttle.markSent(payload, now) } },
            sendParkedEvent = { sendEvent(EventType.VEHICLE_PARKED, JsonObject(emptyMap())) },
        )
        if (!announced && sessionId != null && throttle.shouldSend(payload, now)) sendContext(payload)
    }

    private fun showNextHeldPreview() {
        if (_parking.value !is ParkingDetector.State.Parked || _approval.value != null) return
        heldPreviews.removeFirstOrNull()?.let { _approval.value = it }
        _heldStories.value = heldPreviews.size
    }

    /** Local question (no server command). Not ACKed because it has no command_id. */
    private fun localAsk(text: String) {
        if (navGuidanceActive || speaker.isSpeaking) return
        speaker.speak(text, object : DjSpeaker.Callback {
            override fun onStarted() {}
            override fun onCompleted() = post { speechInput.listenOnce() }
            override fun onInterrupted() {}
            override fun onFailed(reason: String) = post { status("ask.fail $reason") }
        })
    }

    // ---------------- helpers ----------------

    private fun sendContext(p: VehicleContextPayload): Boolean {
        status("context.updated motion=${p.vehicle.motionState} speed=${p.vehicle.speedKmh} wiper=${p.vehicle.wiperActive} eta=${p.navigation.etaSeconds} parked=${p.vehicle.parkedConfirmed} src=${p.sourceMode}")
        return sendEvent(EventType.CONTEXT_UPDATED, p.toJsonElement())
    }

    private fun ack(commandId: String, st: AckStatus) {
        status("ack ${commandId.take(8)} $st")
        sendEvent(EventType.COMMAND_ACK, CommandAckPayload(commandId, st).toJsonElement())
    }

    /** Returns whether the socket accepted the event (false while disconnected). */
    private fun sendEvent(type: String, payload: kotlinx.serialization.json.JsonElement): Boolean {
        val sid = sessionId ?: return false
        val ok = client.send(EventEnvelope.create(sid, type, payload))
        if (!ok) status("send.failed $type")
        return ok
    }

    private fun <T> decode(c: CommandEnvelope, ser: kotlinx.serialization.KSerializer<T>): T? =
        runCatching { ContractJson.decodeFromJsonElement(ser, c.payload) }
            .getOrElse { status("cmd.bad_payload ${c.type}"); ack(c.commandId, AckStatus.FAILED); null }

    private fun post(block: () -> Unit) { scope.launch { block() } }

    fun status(msg: String) {
        Log.i(TAG, msg)
        _log.update { (it + "${java.time.LocalTime.now().withNano(0)} $msg").takeLast(200) }
    }

    /** Debug simulator hook (Virtual Console has no navigation/location replay). */
    fun simulate(transform: (VehicleSignals) -> VehicleSignals) = hub.update { transform(it).copy(simulated = true) }

    private companion object {
        const val TAG = "Roadpod"
        const val LINE_GAP_MS = 800L   // pause between two DJ lines
    }
}
