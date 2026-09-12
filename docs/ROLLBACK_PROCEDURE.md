# Canary Rollback Procedure

## 1. When to Roll Back (Rollback Criteria)

Initiate rollback immediately if:
1. **Critical Clinical Safety Failure**: Any unhandled or misrouted medical advice.
2. **5xx Error Rate > 1%**: Over a 5-minute rolling window.
3. **P95 Latency Regression**: Total turn latency exceeds 2,000ms consistently.
4. **Telephony Audio Corruption**: Dead air, persistent static, or continuous un-interruptible audio playback.

---

## 2. Fast Rollback Execution Steps

### Step 1: Telephony Traffic Re-routing (Time to execute: < 30 seconds)
Re-point the Twilio phone number away from the canary endpoint:
1. Open the **Twilio Console** > **Phone Numbers** > **Manage Numbers**.
2. Select the canary phone number.
3. Under **Voice & Fax > A Call Comes In**:
   - Change Webhook URL to the **previous stable environment** OR a fallback IVR/TwiML bin:
     ```xml
     <Response>
         <Say>We are currently experiencing technical difficulties. Connecting you to an agent.</Say>
         <Dial>+18005550199</Dial>
     </Response>
     ```
4. Save configuration. Inbound calls are immediately rerouted.

### Step 2: Container Stack Rollback (Time to execute: < 60 seconds)
To stop the canary stack and revert to the previous container release:
```bash
# Stop canary services
docker compose -f docker/docker-compose.canary.yml down

# Bring up previous stable release
docker compose -f docker/docker-compose.yml --profile api up -d
```

### Step 3: Database Verification
Canary database changes in Phase 13 were strictly additive (optimistic concurrency version columns).
No schema rollback is needed if reverting application code. If a clean database rollback is required:
```bash
# Roll back one migration step
alembic downgrade -1
```

---

## 3. Post-Rollback Checklist

- [ ] Verify test calls to the Twilio number reach the fallback destination.
- [ ] Inspect error logs for the root cause of the rollback.
- [ ] Archive canary metrics and traces from the incident window.
- [ ] File a post-mortem issue with incident timeline and reproduction steps.