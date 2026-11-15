package ai.roadpod.app.logic

/**
 * Parking decision (decided 2026-11-11):
 *  - only for trips with a destination: starts at NaviHelper onDestinationArrived
 *  - "no vehicle operation" = neither accelerator nor brake pressed
 *  - 5 minutes of no operation after arrival → parked
 *  - a voice question ("주차 완료하셨나요?") is asked while waiting
 *
 * Accelerator unavailable (confirmed on the Pleos emulator) → decided 2026-11-11: judge by gear P.
 *  [AccelFallback.GEAR_P]: "no operation" = gear is P (AAOS GEAR_SELECTION) and brake not pressed.
 *  Leaving P or pressing the brake restarts the 5-minute timer. Gear unknown → Undetermined.
 *
 * Still undecided (kept as config):
 *  - [askAfterMs]: when to ask. null = do not ask until decided.
 */
class ParkingDetector(
    private val quietPeriodMs: Long = 5 * 60 * 1000L,
    private val askAfterMs: Long? = null,
    private val accelUnsupportedFallback: AccelFallback = AccelFallback.GEAR_P,
) {
    enum class AccelFallback { NONE, BRAKE_ONLY, GEAR_P }

    sealed interface State {
        data object NotArrived : State
        data class Waiting(val quietForMs: Long, val remainingMs: Long) : State
        data object Parked : State
        /** Required signals unavailable for the configured rule. */
        data object Undetermined : State
    }

    data class Input(
        val nowMs: Long,
        val arrivedAtMs: Long?,
        val accelSupported: Boolean?,
        val accelPressed: Boolean?,   // percent > 0
        val brakePressed: Boolean?,
        val gearPark: Boolean? = null, // null = gear unknown / unsupported
    )

    private var arrivedAt: Long? = null
    private var lastActivityAt: Long? = null
    private var asked = false
    private var parked = false

    /** Returns the new state and whether the "주차 완료하셨나요?" question is due right now (fires once). */
    fun evaluate(input: Input): Pair<State, Boolean> {
        if (input.arrivedAtMs == null) { reset(); return State.NotArrived to false }
        if (arrivedAt != input.arrivedAtMs) {           // new arrival
            arrivedAt = input.arrivedAtMs; lastActivityAt = input.arrivedAtMs; asked = false; parked = false
        }
        if (parked) return State.Parked to false

        val accelKnown = input.accelSupported == true
        val brake = input.brakePressed == true
        val active = when {
            accelKnown -> brake || input.accelPressed == true
            accelUnsupportedFallback == AccelFallback.BRAKE_ONLY -> brake
            accelUnsupportedFallback == AccelFallback.GEAR_P -> {
                val p = input.gearPark ?: return State.Undetermined to false
                brake || !p
            }
            else -> return State.Undetermined to false
        }
        if (active) { lastActivityAt = input.nowMs; asked = false }

        val quietFor = input.nowMs - (lastActivityAt ?: input.nowMs)
        if (quietFor >= quietPeriodMs) { parked = true; return State.Parked to false }

        var askNow = false
        if (askAfterMs != null && !asked && quietFor >= askAfterMs) { asked = true; askNow = true }
        return State.Waiting(quietFor, quietPeriodMs - quietFor) to askNow
    }

    /** Explicit "yes, parked" answer from the driver could be wired here once decided. */
    fun reset() { arrivedAt = null; lastActivityAt = null; asked = false; parked = false }
}
