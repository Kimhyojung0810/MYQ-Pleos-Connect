package ai.roadpod.app.vehicle

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update

/** Single place where every source writes; consumers observe [signals]. Thread-safe via StateFlow.update. */
class SignalHub {
    private val _signals = MutableStateFlow(VehicleSignals())
    val signals: StateFlow<VehicleSignals> = _signals

    fun update(transform: (VehicleSignals) -> VehicleSignals) = _signals.update(transform)
}
