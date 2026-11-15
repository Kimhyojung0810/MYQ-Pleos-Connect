package ai.roadpod.app

import ai.roadpod.app.contract.ContractJson
import ai.roadpod.app.contract.UiState
import ai.roadpod.app.contract.UserActionType
import ai.roadpod.app.logic.ParkingDetector
import ai.roadpod.app.ui.QrCode
import ai.roadpod.app.vehicle.CarPropertySource
import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.os.Bundle
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import java.io.ByteArrayOutputStream

/**
 * 큐큐 차량 화면. The screen itself is assets/headunit/index.html (the same design as the browser demo),
 * shown in a WebView. This activity only:
 *   - pushes the controller's state to it:   HU.update(state), HU.topics(list)
 *   - turns its buttons into controller calls: Android.action(name, arg)
 * Touch controls work only while stopped or parked (also re-checked here); voice is always available.
 * Story approval and the diary QR appear only after parking.
 */
class MainActivity : AppCompatActivity() {
    private val controller get() = (application as RoadpodApp).controller
    private lateinit var web: WebView
    private var pageReady = false
    private var renderQueued = false
    private var lastTopics: String? = null
    private var qrFor: String? = null
    private var qrDataUrl: String? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#0a1730"))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false          // assets are still readable via file:///android_asset
            addJavascriptInterface(Bridge(), "Android")
            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView, url: String) {
                    pageReady = true
                    lastTopics = null
                    render()
                }
            }
            loadUrl("file:///android_asset/headunit/index.html")
        }
        setContentView(web)

        requestNeededPermissions()

        val c = controller
        // Any change → one render on the next frame (state is read from .value, so collectors only signal).
        listOf<Flow<*>>(c.uiState, c.hub.signals, c.parking, c.approval, c.diary, c.caption, c.conn, c.onAir,
            c.topics, c.music, c.log, c.heldStories, c.storyMode).forEach { flow ->
            lifecycleScope.launch { flow.collect { scheduleRender() } }
        }
    }

    private fun scheduleRender() {
        if (renderQueued) return
        renderQueued = true
        web.post { renderQueued = false; render() }
    }

    private fun isMoving(speedKmh: Double?) = speedKmh != null && kotlin.math.abs(speedKmh) >= 0.5
    private fun isParked() = controller.parking.value is ParkingDetector.State.Parked
    private fun stopped() = isParked() || !isMoving(controller.hub.signals.value.speedKmh)

    private fun render() {
        if (!pageReady) return
        val c = controller
        val s = c.hub.signals.value
        val parked = isParked()
        val ui = c.uiState.value
        val music = c.music.value
        val now = c.onAir.value?.let { runCatching { ContractJson.parseToJsonElement(it) }.getOrNull() }
        val mode = when {
            ui == UiState.NAV_SUSPENDED -> "nav"
            ui == UiState.LISTENING -> "listening"
            ui == UiState.SPEAKING -> "speaking"
            music != null -> "music"
            parked || ui == UiState.PARKED -> "parked"
            ui == UiState.THINKING -> "thinking"
            else -> "idle"
        }
        val approval = if (parked) c.approval.value else null      // 운전 중에는 승인 화면을 띄우지 않는다
        // 운전 중에는 승인을 기다리는 사연 개수만 보여 준다 (주차하면 하나씩 승인 창으로)
        val heldStories = c.heldStories.value + if (!parked && c.approval.value != null) 1 else 0
        val diary = c.diary.value?.let { d -> qrFor(d.viewUrl)?.let { d.viewUrl to it } }
        val state = buildJsonObject {
            put("conn", c.conn.value)
            put("mode", mode)
            put("stopped", stopped())
            s.etaSeconds?.let { put("eta_seconds", it) }
            putJsonObject("caption") {
                val cap = c.caption.value
                put("who", cap.who); put("text", cap.text); put("kind", cap.kind)
            }
            if (now != null) put("now", now)
            if (music != null) putJsonObject("music") { put("title", music) }
            if (approval != null) putJsonObject("approval") { put("text", approval.previewText) }
            if (heldStories > 0) put("held_stories", heldStories)
            if (c.storyMode.value || c.shareModeEnabled) put("story_mode", true)
            if (diary != null) putJsonObject("diary") {
                put("url", diary.first); put("qr", diary.second)
                c.diary.value?.imageUrl?.let { put("image", it) }   // illustration above the QR
            }
            if (BuildConfig.DEBUG) put("debug", JsonArray(debugLines().map { JsonPrimitive(it) }))
        }
        js("window.HU && HU.update(${state})")
        val topics = c.topics.value
        if (topics != null && topics != lastTopics) {
            lastTopics = topics
            // Re-serialized, never pasted raw: only valid JSON reaches the page's JavaScript.
            runCatching { ContractJson.parseToJsonElement(topics) }.getOrNull()?.let { js("window.HU && HU.topics(${it})") }
        }
    }

    private fun debugLines(): List<String> {
        val s = controller.hub.signals.value
        return listOf(
            "speed=${s.speedKmh}  gear=${s.gearSelection}  navi=${s.navStatus}  eta=${s.etaSeconds}s  simulated=${s.simulated}",
            "parking=${controller.parking.value.javaClass.simpleName}  ui=${controller.uiState.value}",
            "",
        ) + controller.log.value.takeLast(40).reversed()
    }

    private fun js(code: String) = web.evaluateJavascript(code, null)

    /** QR as a PNG data URL, made once per diary link. */
    private fun qrFor(url: String): String? {
        if (url == qrFor) return qrDataUrl
        val png = runCatching {
            val bmp = QrCode.bitmap(url)
            ByteArrayOutputStream().use { out -> bmp.compress(Bitmap.CompressFormat.PNG, 100, out); out.toByteArray() }
        }.getOrElse { controller.status("diary.qr_failed ${it.javaClass.simpleName}"); return null }
        qrFor = url
        qrDataUrl = "data:image/png;base64," + Base64.encodeToString(png, Base64.NO_WRAP)
        return qrDataUrl
    }

    /** Called from the page (JavaBridge thread) → handled on the main thread. */
    private inner class Bridge {
        @JavascriptInterface
        fun action(name: String, arg: String) {
            runOnUiThread { onAction(name, arg) }
        }
    }

    private fun onAction(name: String, arg: String) {
        val c = controller
        when (name) {
            "mic" -> c.listenOnce()
            "approve" -> if (isParked()) c.answerStory(true)
            "reject" -> if (isParked()) c.answerStory(false)
            "diaryClose" -> c.dismissDiary()
            "end" -> if (stopped()) { RoadpodService.stop(this); finishAndRemoveTask() }
            // Touch controls: stopped or parked only (the page greys them out too; checked again here).
            "skip" -> if (stopped()) c.sendAction(UserActionType.SKIP_TOPIC)
            "story" -> if (stopped()) c.requestStory()
            "relay" -> if (stopped()) c.sendAction(UserActionType.PLAY_RELAY)
            "topic" -> if (stopped() && arg.isNotBlank()) c.selectTopic(arg)
            else -> c.status("ui.unknown_action $name")
        }
        scheduleRender()
    }

    private fun requestNeededPermissions() {
        val wanted = arrayOf(Manifest.permission.RECORD_AUDIO, CarPropertySource.PERMISSION_SPEED)
        val missing = wanted.filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isEmpty()) startController() else requestPermissions(missing.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        permissions.forEachIndexed { i, p -> controller.status("permission $p granted=${grantResults.getOrNull(i) == PackageManager.PERMISSION_GRANTED}") }
        startController()
    }

    private var started = false
    private fun startController() {
        if (started) return
        started = true
        // Foreground service owns the controller: the app is frozen otherwise when navigation comes to the front.
        RoadpodService.start(this)
    }

    override fun onDestroy() {
        web.removeJavascriptInterface("Android")
        web.destroy()
        super.onDestroy()
    }
}
