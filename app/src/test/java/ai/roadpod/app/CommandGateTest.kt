package ai.roadpod.app

import ai.roadpod.app.command.CommandGate
import ai.roadpod.app.contract.CommandEnvelope
import ai.roadpod.app.contract.ContractJson
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.OffsetDateTime

class CommandGateTest {
    private val now = OffsetDateTime.parse("2026-11-11T08:00:00+09:00")

    private fun speak(id: String, policy: String = "DEFER_IF_NAV", expires: OffsetDateTime = now.plusSeconds(20), session: String = "s1") =
        CommandEnvelope(
            schemaVersion = "1.0", commandId = id, sessionId = session, type = "dj.speak",
            issuedAt = now.toString(), expiresAt = expires.toString(),
            payload = ContractJson.parseToJsonElement("""{"text":"hi","voice_style":"WARM","interrupt_policy":"$policy"}""").jsonObject,
        )

    @Test fun executesValidCommand() {
        assertTrue(CommandGate().evaluate(speak("a"), "s1", now, false) is CommandGate.Decision.Execute)
    }

    @Test fun duplicateCommandIdIsIgnored_T05() {
        val g = CommandGate()
        g.evaluate(speak("a"), "s1", now, false)
        assertTrue(g.evaluate(speak("a"), "s1", now, false) is CommandGate.Decision.Duplicate)
    }

    @Test fun expiredCommandIsDropped_T15() {
        val d = CommandGate().evaluate(speak("a", expires = now.minusSeconds(1)), "s1", now, false)
        assertTrue(d is CommandGate.Decision.Drop && d.reason == "EXPIRED")
    }

    @Test fun navActiveDefersOrDrops_T06() {
        val g = CommandGate()
        assertTrue(g.evaluate(speak("a", "DEFER_IF_NAV"), "s1", now, true) is CommandGate.Decision.Defer)
        val d = g.evaluate(speak("b", "DROP_IF_NAV"), "s1", now, true)
        assertTrue(d is CommandGate.Decision.Drop && d.reason == "NAV_ACTIVE")
    }

    @Test fun otherSessionIsDropped_T02() {
        val d = CommandGate().evaluate(speak("a", session = "other"), "s1", now, false)
        assertTrue(d is CommandGate.Decision.Drop && d.reason == "SESSION_MISMATCH")
    }

    @Test fun deferredExpiresBeforeResume_T07() {
        val g = CommandGate()
        val c = speak("a", expires = now.plusSeconds(5))
        assertTrue(g.stillValid(c, now.plusSeconds(4)))
        assertTrue(!g.stillValid(c, now.plusSeconds(6)))
    }

    @Test fun nonSpeakCommandsAreNotGatedByNav() {
        val c = speak("p").copy(type = "media.pause", payload = JsonObject(emptyMap()))
        assertTrue(CommandGate().evaluate(c, "s1", now, true) is CommandGate.Decision.Execute)
    }
}
