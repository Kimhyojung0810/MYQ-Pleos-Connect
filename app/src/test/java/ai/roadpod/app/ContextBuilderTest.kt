package ai.roadpod.app

import ai.roadpod.app.contract.ContractJson
import ai.roadpod.app.contract.EventEnvelope
import ai.roadpod.app.contract.EventType
import ai.roadpod.app.contract.MotionState
import ai.roadpod.app.contract.SourceMode
import ai.roadpod.app.contract.toJsonElement
import ai.roadpod.app.logic.AttentionEvaluator
import ai.roadpod.app.logic.ContextBuilder
import ai.roadpod.app.vehicle.VehicleSignals
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.OffsetDateTime

class ContextBuilderTest {
    private val b = ContextBuilder()

    @Test fun unknownWhenNoSignals_T08() {
        val p = b.build(VehicleSignals(), parkedConfirmed = false, runningOnEmulator = false)
        assertEquals(MotionState.UNKNOWN, p.vehicle.motionState)
        assertEquals(null, p.vehicle.speedKmh)
        assertEquals(null, p.vehicle.wiperActive)
        assertEquals(SourceMode.REAL, p.sourceMode)
    }

    @Test fun zeroIsNotNull() {
        val p = b.build(VehicleSignals(speedKmh = 0.0), false, false)
        assertEquals(MotionState.STOPPED, p.vehicle.motionState)
        assertEquals(0.0, p.vehicle.speedKmh)
    }

    @Test fun reverseSpeedSentAsNullUntilDecided() {
        val p = b.build(VehicleSignals(speedKmh = -5.0), false, false)
        assertEquals(MotionState.MOVING, p.vehicle.motionState)
        assertEquals(null, p.vehicle.speedKmh)
    }

    @Test fun parkedWins() {
        assertEquals(MotionState.PARKED, b.build(VehicleSignals(speedKmh = 0.0), true, true).vehicle.motionState)
    }

    @Test fun jsonHasSchemaKeysIncludingNulls() {
        val p = b.build(VehicleSignals(speedKmh = 42.0, wiperOn = true, etaSeconds = 600), false, true)
        val obj = p.toJsonElement().jsonObject
        assertEquals(setOf("commute_type", "vehicle", "navigation", "traffic", "weather", "source_mode"), obj.keys)
        assertTrue("road_name" in obj["navigation"]!!.jsonObject)          // explicit null present
        assertEquals("SIMULATED", obj["source_mode"].toString().trim('"'))

        // Write a full envelope so the integration step can validate it with the real JSON Schema.
        val env = EventEnvelope.create("s1", EventType.CONTEXT_UPDATED, obj, OffsetDateTime.parse("2026-11-11T08:00:00+09:00"))
        File("build/contract-samples").mkdirs()
        File("build/contract-samples/context_updated.json").writeText(ContractJson.encodeToString(EventEnvelope.serializer(), env))
        val env2 = EventEnvelope.create("s1", EventType.CONTEXT_UPDATED, b.build(VehicleSignals(), false, false).toJsonElement())
        File("build/contract-samples/context_updated_unknown.json").writeText(ContractJson.encodeToString(EventEnvelope.serializer(), env2))
    }

    @Test fun throttleSendsOnlyChanges() {
        val t = ContextBuilder.SendThrottle(minIntervalMs = 1000)
        val p1 = b.build(VehicleSignals(speedKmh = 10.0), false, false)
        assertTrue(t.shouldSend(p1, 0))
        assertTrue(!t.shouldSend(p1, 5000))                                  // unchanged
        val p2 = b.build(VehicleSignals(speedKmh = 20.0), false, false)
        assertTrue(t.shouldSend(p2, 5500))                                   // changed, 5.5s since last send
        val p3 = b.build(VehicleSignals(speedKmh = 30.0), false, false)
        assertTrue(!t.shouldSend(p3, 6000))                                  // changed but only 0.5s since last send
        assertTrue(t.shouldSend(p3, 6600))                                   // retried on a later tick
    }

    @Test fun attentionFlags() {
        val a = AttentionEvaluator()
        val r = a.evaluate(VehicleSignals(speedKmh = 70.0, roadSpeedLimitKmh = 60, cameraRemainDistanceM = 200, cameraSpeedLimitKmh = 60))
        assertTrue(r.attentionRequired)
        assertTrue(AttentionEvaluator.Reason.OVER_CAMERA_LIMIT in r.reasons)
        val s = a.evaluate(VehicleSignals(speedKmh = 0.0))
        assertTrue(s.stoppedSegment && !s.attentionRequired)
        val c = a.evaluate(VehicleSignals(speedKmh = 30.0, congestionAheadOffsetM = 100.0, congestionAheadAvgSpeedKmh = 8.0))
        assertTrue(c.stoppedSegment)
    }
}
