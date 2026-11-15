package ai.roadpod.app.vehicle

import ai.pleos.playground.vehicle.Vehicle
import ai.pleos.playground.vehicle.api.model.area.WindowArea
import ai.pleos.playground.vehicle.constant.area.WindowValue
import ai.pleos.playground.vehicle.constant.control.VehicleGear
import ai.pleos.playground.vehicle.constant.control.WiperState
import ai.pleos.playground.vehicle.listener.BrakePedalStatusListener
import ai.pleos.playground.vehicle.listener.GearSelectionListener
import ai.pleos.playground.vehicle.listener.WiperStateListener
import android.content.Context

/** Pleos Vehicle SDK (state only): brake pedal, front wiper, gear (gear is logged only). */
class PleosVehicleSource(
    context: Context,
    private val hub: SignalHub,
    private val onActivity: () -> Unit,        // brake pressed → parking timer reset
    private val onStatus: (String) -> Unit = {},
) {
    private val vehicle = Vehicle(context)
    private val frontWindshield = WindowArea(WindowValue.FRONT_WINDSHIELD)

    private val brakeListener = object : BrakePedalStatusListener {
        override fun onBrakePedalStatusUpdated(value: Boolean?) {
            hub.update { it.copy(brakePressed = value) }
            if (value == true) onActivity()
        }
        override fun onFailed(e: Exception) { onStatus("brake.listener_failed: ${e.message}") }
    }

    private val wiperListener = object : WiperStateListener {
        override fun onWiperStateUpdated(value: WiperState?) {
            hub.update { it.copy(wiperOn = wiperOnOrNull(value)) }
        }
        override fun onFailed(e: Exception) { onStatus("wiper.listener_failed: ${e.message}") }
    }

    private val gearListener = object : GearSelectionListener {
        override fun onSelected(value: VehicleGear?) {
            hub.update { it.copy(gear = value?.name) }
        }
        override fun onFailed(e: Exception) { onStatus("gear.listener_failed: ${e.message}") }
    }

    fun start() {
        runCatching { vehicle.initialize() }.onFailure { onStatus("vehicle.init_failed: ${it.message}"); return }
        onStatus("vehicle.initialized")

        val brake = vehicle.getBrake()
        brake.getCurrentBrakePedalStatus({ v -> hub.update { it.copy(brakePressed = v) } }, { onStatus("brake.get_failed: ${it.message}") })
        runCatching { brake.registerBrakePedalStatus(brakeListener) }.onFailure { onStatus("brake.register_failed: ${it.message}") }

        val wiper = vehicle.getWiper()
        wiper.getCurrentWiperState(frontWindshield, { v -> hub.update { it.copy(wiperOn = wiperOnOrNull(v)) } }, { onStatus("wiper.get_failed: ${it.message}") })
        runCatching { wiper.registerWiperState(frontWindshield, wiperListener) }.onFailure { onStatus("wiper.register_failed: ${it.message}") }

        val gear = vehicle.getGear()
        gear.getCurrentGearSelectionState({ v -> hub.update { it.copy(gear = v?.name) } }, { onStatus("gear.get_failed: ${it.message}") })
        runCatching { gear.registerGearSelection(gearListener) }.onFailure { onStatus("gear.register_failed: ${it.message}") }
    }

    fun stop() {
        runCatching { vehicle.getBrake().unregisterBrakePedalStatus(brakeListener) }
        runCatching { vehicle.getWiper().unregisterWiperState(frontWindshield, wiperListener) }
        runCatching { vehicle.getGear().unregisterGearSelection(gearListener) }
        runCatching { vehicle.release() }
    }

    companion object {
        /** ON → true, OFF → false; OTHER/SERVICE are not a clear on/off → null. */
        fun wiperOnOrNull(state: WiperState?): Boolean? = when (state) {
            WiperState.ON -> true
            WiperState.OFF -> false
            else -> null
        }
    }
}
