package ai.roadpod.app.vehicle

import android.car.Car
import android.car.VehiclePropertyIds
import android.car.hardware.CarPropertyValue
import android.car.hardware.property.CarPropertyManager
import android.content.Context

/**
 * AAOS CarPropertyManager: vehicle speed (Pleos FAQ: PERF_VEHICLE_SPEED) and accelerator pedal.
 * Support is checked at runtime via getCarPropertyConfig; unsupported → *Supported=false, value null.
 */
class CarPropertySource(
    private val context: Context,
    private val hub: SignalHub,
    private val onActivity: () -> Unit,        // accelerator pressed → parking timer reset
    private val onStatus: (String) -> Unit = {},
) {
    private var car: Car? = null
    private var cpm: CarPropertyManager? = null

    private val callback = object : CarPropertyManager.CarPropertyEventCallback {
        override fun onChangeEvent(value: CarPropertyValue<*>) {
            when (value.propertyId) {
                VehiclePropertyIds.PERF_VEHICLE_SPEED -> {
                    val ms = (value.value as? Float)?.toDouble()
                    hub.update { if (it.speedLocked) it else it.copy(speedKmh = ms?.let { v -> v * 3.6 }) }
                }
                VehiclePropertyIds.GEAR_SELECTION -> {
                    val g = value.value as? Int
                    hub.update { if (it.gearLocked) it else it.copy(gearSelection = g) }
                }
                ACCELERATOR_PEDAL_COMPRESSION_PERCENTAGE -> {
                    val pct = (value.value as? Float)?.toDouble()
                    hub.update { it.copy(accelPedalPercent = pct) }
                    if (pct != null && pct > 0.0) onActivity()
                }
            }
        }

        override fun onErrorEvent(propId: Int, zone: Int) {
            onStatus("carprop.error prop=$propId")
        }
    }

    fun start() {
        val c = runCatching { Car.createCar(context) }.getOrNull()
        if (c == null) { onStatus("car.create_failed"); hub.update { it.copy(speedSupported = false, accelSupported = false) }; return }
        car = c
        val m = c.getCarManager(Car.PROPERTY_SERVICE) as? CarPropertyManager
        cpm = m
        if (m == null) { onStatus("carprop.unavailable"); return }

        val speedOk = register(m, VehiclePropertyIds.PERF_VEHICLE_SPEED, 5f)
        val accelOk = register(m, ACCELERATOR_PEDAL_COMPRESSION_PERCENTAGE, 5f)
        // GEAR_SELECTION is ON_CHANGE: rate 0
        val gearOk = register(m, VehiclePropertyIds.GEAR_SELECTION, 0f)
        hub.update { it.copy(speedSupported = speedOk, accelSupported = accelOk, gearSupported = gearOk) }
        onStatus("carprop.speed_supported=$speedOk accel_supported=$accelOk gear_supported=$gearOk")
    }

    private fun register(m: CarPropertyManager, propId: Int, rateHz: Float): Boolean {
        val config = runCatching { m.getCarPropertyConfig(propId) }.getOrNull() ?: return false
        val rate = rateHz.coerceIn(config.minSampleRate, maxOf(config.maxSampleRate, config.minSampleRate))
        return runCatching { m.registerCallback(callback, propId, rate) }
            .onFailure { onStatus("carprop.register_failed prop=$propId: ${it.javaClass.simpleName}") }
            .getOrDefault(false)
    }

    fun stop() {
        runCatching { cpm?.unregisterCallback(callback) }
        runCatching { car?.disconnect() }
    }

    companion object {
        /** Not present in the API 34 stubs; raw id from VehiclePropertyIds (later Android releases). */
        const val ACCELERATOR_PEDAL_COMPRESSION_PERCENTAGE = 291504911
        const val PERMISSION_SPEED = "android.car.permission.CAR_SPEED"
        /** android.car.VehicleGear.GEAR_PARK */
        const val GEAR_PARK = 0x0004
    }
}
