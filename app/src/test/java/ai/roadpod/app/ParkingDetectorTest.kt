package ai.roadpod.app

import ai.roadpod.app.logic.ParkingDetector
import ai.roadpod.app.logic.ParkingDetector.Input
import ai.roadpod.app.logic.ParkingDetector.State
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ParkingDetectorTest {
    private val min = 60_000L

    private fun inp(now: Long, arrived: Long? = 0L, accelSup: Boolean? = true, accel: Boolean? = false, brake: Boolean? = false, park: Boolean? = null) =
        Input(now, arrived, accelSup, accel, brake, park)

    @Test fun notArrivedWithoutDestination() {
        assertEquals(State.NotArrived, ParkingDetector().evaluate(inp(0, arrived = null)).first)
    }

    @Test fun parkedAfterFiveQuietMinutes() {
        val d = ParkingDetector()
        assertTrue(d.evaluate(inp(4 * min)).first is State.Waiting)
        assertEquals(State.Parked, d.evaluate(inp(5 * min)).first)
    }

    @Test fun brakeOrAccelResetsTimer_T11() {
        val d = ParkingDetector()
        d.evaluate(inp(4 * min, brake = true))                 // activity at 4 min
        assertTrue(d.evaluate(inp(8 * min)).first is State.Waiting)  // only 4 min quiet
        d.evaluate(inp(8 * min, accel = true))                 // activity at 8 min
        assertTrue(d.evaluate(inp(12 * min)).first is State.Waiting)
        assertEquals(State.Parked, d.evaluate(inp(13 * min)).first)
    }

    @Test fun undeterminedWhenAccelUnsupportedAndNoFallback() {
        val d = ParkingDetector(accelUnsupportedFallback = ParkingDetector.AccelFallback.NONE)
        assertEquals(State.Undetermined, d.evaluate(inp(10 * min, accelSup = false, park = true)).first)
    }

    // ---- default fallback: gear P (decided 2026-11-11) ----

    @Test fun gearP_parkedAfterFiveMinutesInP_T12() {
        val d = ParkingDetector()
        assertTrue(d.evaluate(inp(4 * min, accelSup = false, park = true)).first is State.Waiting)
        assertEquals(State.Parked, d.evaluate(inp(5 * min, accelSup = false, park = true)).first)
    }

    @Test fun gearP_notParkedWhileInDrive() {
        val d = ParkingDetector()
        assertTrue(d.evaluate(inp(10 * min, accelSup = false, park = false)).first is State.Waiting)
    }

    @Test fun gearP_leavingPOrBrakeRestartsTimer_T11() {
        val d = ParkingDetector()
        d.evaluate(inp(3 * min, accelSup = false, park = false))                 // still in D at 3 min
        assertTrue(d.evaluate(inp(7 * min, accelSup = false, park = true)).first is State.Waiting)
        d.evaluate(inp(7 * min, accelSup = false, park = true, brake = true))   // brake at 7 min
        assertTrue(d.evaluate(inp(11 * min, accelSup = false, park = true)).first is State.Waiting)
        assertEquals(State.Parked, d.evaluate(inp(12 * min, accelSup = false, park = true)).first)
    }

    @Test fun gearP_undeterminedWhenGearUnknown() {
        assertEquals(State.Undetermined, ParkingDetector().evaluate(inp(10 * min, accelSup = false, park = null)).first)
    }

    @Test fun accelSupportedUsesOriginalRule() {
        val d = ParkingDetector()   // accelerator available → original accel/brake rule, gear ignored
        assertEquals(State.Parked, d.evaluate(inp(5 * min, accelSup = true, park = false)).first)
    }

    @Test fun brakeOnlyFallbackWhenConfigured() {
        val d = ParkingDetector(accelUnsupportedFallback = ParkingDetector.AccelFallback.BRAKE_ONLY)
        assertEquals(State.Parked, d.evaluate(inp(5 * min, accelSup = false)).first)
    }

    @Test fun askFiresOnceWhenConfigured() {
        val d = ParkingDetector(askAfterMs = 2 * min)
        assertEquals(false, d.evaluate(inp(1 * min)).second)
        assertEquals(true, d.evaluate(inp(2 * min)).second)
        assertEquals(false, d.evaluate(inp(3 * min)).second)
    }

    @Test fun askDisabledByDefault() {
        val d = ParkingDetector()
        assertEquals(false, d.evaluate(inp(4 * min)).second)
    }

    @Test fun newArrivalRestarts() {
        val d = ParkingDetector()
        assertEquals(State.Parked, d.evaluate(inp(5 * min)).first)
        assertTrue(d.evaluate(inp(6 * min, arrived = 6 * min)).first is State.Waiting)
    }
}
