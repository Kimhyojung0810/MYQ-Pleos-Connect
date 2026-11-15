package ai.roadpod.app.logic

/**
 * Tells the server once per parking that the car is parked.
 *
 * The server accepts `vehicle.parked` only when the latest `context.updated` already has
 * `parked_confirmed: true` (engine.py), and silently ignores it otherwise. So the parked context
 * always goes first, then `vehicle.parked`. Done is recorded only when both sends were accepted,
 * so a send lost to a socket drop is retried on the next tick.
 */
class ParkedAnnouncer {
    private var done = false

    /** Returns true when the announcement went out on this call. Senders return whether the socket accepted. */
    fun update(parked: Boolean, sendParkedContext: () -> Boolean, sendParkedEvent: () -> Boolean): Boolean {
        if (!parked) { done = false; return false }
        if (done) return false
        if (!sendParkedContext()) return false
        if (!sendParkedEvent()) return false
        done = true
        return true
    }

    /** After a reconnect the server may have missed it; announce again (the server makes one diary per session). */
    fun resend() { done = false }
}
