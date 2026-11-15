package ai.roadpod.app.command

import ai.roadpod.app.contract.CommandEnvelope
import ai.roadpod.app.contract.CommandType
import ai.roadpod.app.contract.ContractJson
import ai.roadpod.app.contract.DjSpeakPayload
import ai.roadpod.app.contract.InterruptPolicy
import ai.roadpod.app.contract.SCHEMA_VERSION
import java.time.OffsetDateTime

/**
 * Local safety gate for server commands (contract §5, tests T05/T06/T07/T15).
 * Pure logic, no Android dependencies, so it is unit-tested directly.
 */
class CommandGate(
    private val maxRemembered: Int = 500,
) {
    sealed interface Decision {
        data class Execute(val command: CommandEnvelope) : Decision
        /** dj.speak while navigation guidance is active with DEFER_IF_NAV. */
        data class Defer(val command: CommandEnvelope) : Decision
        data class Drop(val command: CommandEnvelope, val reason: String) : Decision
        /** Duplicate command_id: ignore silently, no second ACK side effects. */
        data class Duplicate(val commandId: String) : Decision
    }

    private val seen = LinkedHashSet<String>()

    fun evaluate(
        command: CommandEnvelope,
        sessionId: String,
        now: OffsetDateTime,
        navGuidanceActive: Boolean,
    ): Decision {
        if (!seen.add(command.commandId)) return Decision.Duplicate(command.commandId)
        if (seen.size > maxRemembered) seen.remove(seen.first())

        if (command.schemaVersion != SCHEMA_VERSION) return Decision.Drop(command, "SCHEMA_VERSION")
        if (command.sessionId != sessionId) return Decision.Drop(command, "SESSION_MISMATCH")
        val expires = runCatching { OffsetDateTime.parse(command.expiresAt) }.getOrNull()
            ?: return Decision.Drop(command, "BAD_EXPIRES_AT")
        if (!now.isBefore(expires)) return Decision.Drop(command, "EXPIRED")

        if (command.type == CommandType.DJ_SPEAK && navGuidanceActive) {
            val policy = runCatching {
                ContractJson.decodeFromJsonElement(DjSpeakPayload.serializer(), command.payload).interruptPolicy
            }.getOrElse { return Decision.Drop(command, "BAD_PAYLOAD") }
            return when (policy) {
                InterruptPolicy.DEFER_IF_NAV -> Decision.Defer(command)
                InterruptPolicy.DROP_IF_NAV -> Decision.Drop(command, "NAV_ACTIVE")
            }
        }
        return Decision.Execute(command)
    }

    /** Re-check a deferred command when navigation guidance ends. */
    fun stillValid(command: CommandEnvelope, now: OffsetDateTime): Boolean {
        val expires = runCatching { OffsetDateTime.parse(command.expiresAt) }.getOrNull() ?: return false
        return now.isBefore(expires)
    }
}
