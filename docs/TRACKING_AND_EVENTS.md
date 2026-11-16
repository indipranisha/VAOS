# Tracking and Event Rules

VAOS's **Tracking rules** page reuses the current camera detector and its short-term, class-aware overlap tracker. It does not create a second model or identify a person. Track continuity is approximate, local to a camera process, and resets after a restart or a sufficiently long detection gap.

## Create a rule

1. Open **Tracking rules** from the left navigation.
2. Enter a short name and an object label produced by the active detector (for example `person`, `truck`, `bus`, `car`, or `motorcycle`). A custom detector may use other labels.
3. Choose one camera or **All active cameras**.
4. Choose a rule:
   - **Object count in region** alerts when at least the selected number of matching objects are present for the configured number of consecutive frames.
   - **Dwell time in region** alerts when one approximate tracked object remains in the configured region for the requested time. Object tracking must be enabled in engine settings.
5. Set the minimum detection confidence, confirmation frames, and cooldown. Confidence is the detector's score, not a probability that the event is true.
6. Set the rectangle using percentages of the frame. `Left`/`Top` are the upper-left edge; `Right`/`Bottom` are the lower-right edge. The default covers the whole image.
7. Save the rule. It is evaluated on future incoming frames; an idle camera does not generate new alerts.

Rules are limited to 100 saved definitions. Each alert stores its timestamp, camera/rule labels, object class/count, and a short message. It does not store a frame, image, face, or snapshot. The newest 5,000 rule alerts are retained; deleting a rule keeps its past alerts. Clearing activity through the existing Control Room action clears face sightings, object summaries, and rule alerts but does not delete rule definitions.

## REST API

The versioned REST routes are:

- `GET` and `POST /api/v1/admin/tracking/rules`
- `PATCH` and `DELETE /api/v1/admin/tracking/rules/{rule_id}`
- `GET /api/v1/admin/tracking/events?limit=100&before_id=...`

The rule API uses the existing `events` API capability. Rule/event reads return camera capability tokens and therefore require the global sensitive-read setting and a token-specific sensitive-read grant when using a bearer token. MCP writes additionally need the sensitive-action setting, a one-time administrator approval, and the delete setting for deletions. See [API](API.md) and [MCP](MCP.md).

`region` is a normalized rectangle: `{"x":0.0,"y":0.0,"width":1.0,"height":1.0}`. Rule types are `count_in_region` and `dwell_in_region`. The API validates ranges and rejects unknown/inactive camera tokens. `GET` event history supports a maximum `limit` of 500, optional `before_id`, `camera_token`, and `rule_id` filters.

## Accuracy and operational limits

- The tracker matches same-class boxes using overlap and forgets tracks after about five seconds without a match. Occlusion, frame drops, similar nearby objects, camera movement, and model label changes can split or merge tracks.
- Dwell alerts require continuous observed detections. A missing frame resets that track's dwell state, which favors fewer false dwell alerts over bridging occlusions.
- Count alerts can repeat after the cooldown while a condition remains true.
- Rules execute in the existing frame path. Definitions are cached for a few seconds; saves take effect on subsequent frames after that cache refresh.
- The frame evaluator is best-effort: if rule evaluation or alert persistence fails, VAOS logs the error and preserves camera detection output.
- There is no directional line-crossing rule, external notification delivery, snapshot/clip evidence, cross-camera identity, or accuracy certification yet.

The current live services were not restarted to activate this new source while model training was active. The pure evaluator and API security tests are present but have not been run under high host-memory use; camera and browser journeys are still required before this feature can be called release-ready.
