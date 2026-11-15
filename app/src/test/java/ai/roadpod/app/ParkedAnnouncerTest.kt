package ai.roadpod.app

import ai.roadpod.app.logic.ParkedAnnouncer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ParkedAnnouncerTest {
    private val sent = mutableListOf<String>()
    private var socketUp = true
    private val a = ParkedAnnouncer()

    private fun tick(parked: Boolean) = a.update(
        parked,
        sendParkedContext = { if (socketUp) sent += "context(parked=true)"; socketUp },
        sendParkedEvent = { if (socketUp) sent += "vehicle.parked"; socketUp },
    )

    @Test fun parkedContextGoesBeforeVehicleParked() {
        assertTrue(tick(true))
        assertEquals(listOf("context(parked=true)", "vehicle.parked"), sent)
    }

    @Test fun announcesOncePerParking() {
        tick(true); tick(true); tick(true)
        assertEquals(2, sent.size)
    }

    @Test fun nothingWhileNotParked() {
        assertFalse(tick(false))
        assertTrue(sent.isEmpty())
    }

    @Test fun retriesAfterFailedSend() {
        socketUp = false
        assertFalse(tick(true))
        socketUp = true
        assertTrue(tick(true))
        assertEquals(listOf("context(parked=true)", "vehicle.parked"), sent)
    }

    @Test fun resendAfterReconnect() {
        tick(true)
        a.resend()
        assertTrue(tick(true))
        assertEquals(4, sent.size)
    }

    @Test fun newParkingAfterDrivingAnnouncesAgain() {
        tick(true); tick(false)
        assertTrue(tick(true))
        assertEquals(4, sent.size)
    }
}
