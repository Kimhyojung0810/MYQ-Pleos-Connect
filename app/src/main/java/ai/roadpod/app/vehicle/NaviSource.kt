package ai.roadpod.app.vehicle

import ai.pleos.playground.navi.constants.NaviErrorCode
import ai.pleos.playground.navi.constants.NavigationStatus
import ai.pleos.playground.navi.data.CameraAlert
import ai.pleos.playground.navi.data.CurrentLocationInfo
import ai.pleos.playground.navi.data.DestinationArrivedInfo
import ai.pleos.playground.navi.data.RouteStateInfo
import ai.pleos.playground.navi.data.TripInfo
import ai.pleos.playground.navi.helper.NaviHelper
import ai.pleos.playground.navi.helper.listener.NaviHelperEventListener
import android.content.Context
import android.util.Log

/** Pleos NaviHelper → SignalHub. Address/location are never logged (privacy). */
class NaviSource(
    context: Context,
    private val hub: SignalHub,
    private val clock: () -> Long = System::currentTimeMillis,
    private val onArrived: () -> Unit,
    private val onStatus: (String) -> Unit = {},
) {
    private val navi = NaviHelper(context)

    private val listener = object : NaviHelperEventListener {
        override fun onNavigationStatus(status: NavigationStatus) {
            hub.update { it.copy(navStatus = status.name) }
            onStatus("navi.status=${status.name}")
        }

        override fun onTripInfo(tripInfo: TripInfo) {
            hub.update { it.copy(etaSeconds = tripInfo.eta) }
        }

        override fun onCurrentLocationInfo(currentLocationInfo: CurrentLocationInfo) {
            hub.update { it.copy(address = currentLocationInfo.address.ifBlank { null }) }
        }

        override fun onCurrentRoadSpeedLimit(speedLimit: Int) {
            hub.update { it.copy(roadSpeedLimitKmh = speedLimit.takeIf { v -> v > 0 }) }
        }

        override fun onCameraAlert(cameraAlertList: List<CameraAlert>) {
            val nearest = cameraAlertList.filter { it.status }.minByOrNull { it.remainDistance }
            hub.update {
                it.copy(
                    cameraSpeedLimitKmh = nearest?.speedLimit?.takeIf { v -> v > 0 },
                    cameraRemainDistanceM = nearest?.remainDistance,
                    cameraSectionAvgSpeedKmh = nearest?.sectionAvrSpeed?.takeIf { v -> v > 0 },
                )
            }
        }

        override fun onRouteStateInfo(routeStateInfo: RouteStateInfo) {
            val nearest = routeStateInfo.trafficStatus?.congestionItemList?.minByOrNull { it.offset }
            hub.update {
                it.copy(
                    congestionAheadAvgSpeedKmh = nearest?.avrSpeed,
                    congestionAheadOffsetM = nearest?.offset,
                    congestionRoadName = nearest?.roadName?.ifBlank { null },
                )
            }
        }

        override fun onDestinationArrived(destinationArrivedInfo: DestinationArrivedInfo) {
            hub.update { it.copy(destinationArrivedAtMs = clock()) }
            onStatus("navi.destination_arrived")
            onArrived()
        }

        override fun onError(errorCode: NaviErrorCode) {
            onStatus("navi.error=${errorCode.name}")
        }
    }

    fun start() {
        navi.initialize(
            {
                runCatching {
                    navi.addListener(listener)
                    onStatus("navi.connected")
                    navi.getRouteStateInfo()
                }.onFailure { onStatus("navi.call_failed: ${it.javaClass.simpleName} ${it.message}") }
            },
            { t -> Log.w(TAG, "NaviHelper init failed", t); onStatus("navi.init_failed: ${t.message}") },
        )
    }

    fun stop() {
        runCatching { navi.removeListener(listener) }
        runCatching { navi.release() }
    }

    private companion object { const val TAG = "NaviSource" }
}
