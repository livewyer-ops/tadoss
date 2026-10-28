import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const fixture = JSON.parse(open(__ENV.FIXTURE));
const batch = Number(__ENV.BATCH || 100);
const payload = 'x'.repeat(Number(__ENV.PAYLOAD_BYTES || 64));
const mode = __ENV.WORKLOAD || 'mixed';
const events = new Counter('events_committed');
const rejected = new Counter('events_rejected');
const bytes = new Counter('payload_bytes_submitted');
const failures = new Rate('operation_failed');
const responses = new Counter('api_responses');
const latency = new Trend('api_request_duration', true);
let token = '', expires = 0;

export const options = {
  scenarios: { load: {
    executor: 'constant-arrival-rate', rate: Number(__ENV.RATE || 20), timeUnit: '1s',
    duration: __ENV.DURATION || '5m', preAllocatedVUs: Number(__ENV.VUS || 32),
    maxVUs: Number(__ENV.MAX_VUS || 512), gracefulStop: '35s',
  } },
  thresholds: {
    operation_failed: ['rate==0'], dropped_iterations: ['count==0'],
    ...Object.fromEntries([0, 200, 400, 401, 403, 409, 413, 429, 500, 502, 503, 504]
      .map(status => [`api_responses{status:${status}}`, ['count>=0']])),
  },
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
  systemTags: ['status', 'method', 'name', 'scenario', 'error_code'],
};

function record(response) {
  responses.add(1, { status: String(response.status) });
  latency.add(response.timings.duration);
}

function auth() {
  if (Date.now() >= expires) {
    const r = http.post(__ENV.TADOSS_OAUTH_TOKEN_URL, {
      grant_type: 'client_credentials', client_id: __ENV.TADOSS_OAUTH_CLIENT_ID,
      client_secret: __ENV.TADOSS_OAUTH_CLIENT_SECRET, scope: __ENV.TADOSS_OAUTH_SCOPES,
    }, { tags: { name: 'oauth' }, timeout: '10s' });
    if (r.status !== 200) { failures.add(true); throw new Error(`OAuth returned ${r.status}`); }
    token = r.json('access_token');
    expires = Date.now() + Math.max(1, Number(r.json('expires_in') || 300) - 30) * 1000;
  }
  return { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
}

export default function () {
  const iteration = exec.scenario.iterationInTest;
  const track = mode === 'no-overlap' ? fixture.restricted :
    fixture.tracks[mode === 'hot' ? 0 : iteration % fixture.tracks.length];
  const root = `${__ENV.TADOSS_URL}/tracks/${track}/events`;
  const headers = auth();
  const read = mode.startsWith('read') || (mode === 'mixed' && iteration % 4 !== 0);
  if (read) {
    const start = iteration % fixture.events_per_track;
    const query = mode === 'read-page' ? '?limit=100' :
      `?limit=100&timerange=${encodeURIComponent(`[${start}:0_${start + 100}:0)`)}`;
    const r = http.get(root + query, { headers, tags: { name: mode === 'read-page' ? 'page' : 'range' }, timeout: '30s' });
    record(r);
    failures.add(r.status !== 200);
    return;
  }
  const update = mode === 'update' || mode === 'no-overlap' || (mode === 'mixed' && Math.floor(iteration / 4) % 2 === 0);
  const body = [];
  for (let i = 0; i < batch; i++) {
    const index = update ? (iteration * batch + i) % fixture.events_per_track :
      fixture.events_per_track + iteration * batch + i;
    const id = (update ? track : __ENV.RUN_ID).slice(0, 24) +
      (update ? index : iteration * batch + i).toString(16).padStart(12, '0');
    body.push({ id, timerange: `[${index}:0_${index + 1}:0)`, payload: { text: payload } });
  }
  const encoded = JSON.stringify(body);
  if (encoded.length > 1048576) throw new Error('Batch exceeds the 1 MiB body limit');
  bytes.add(encoded.length);
  const r = http.post(root, encoded, { headers, tags: { name: update ? 'update' : 'insert' }, timeout: '30s' });
  record(r);
  if (r.status !== 200) { failures.add(true); return; }
  const results = r.json('responses');
  const accepted = results.filter(x => x.http_status_code === (update ? 200 : 201)).length;
  events.add(accepted);
  rejected.add(batch - accepted);
  failures.add(accepted !== batch);
}

export function handleSummary(data) {
  return { [__ENV.SUMMARY]: JSON.stringify(data, null, 2) };
}
