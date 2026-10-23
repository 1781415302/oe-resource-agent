-- Aggregate-only exporter using the official wrk statistics API.
-- Upstream API: https://github.com/wg/wrk/blob/master/SCRIPTING
local ffi = require("ffi")
ffi.cdef[[
  struct timespec { long tv_sec; long tv_nsec; };
  int clock_gettime(int clock_id, struct timespec *tp);
]]
local function monotonic()
  local ts = ffi.new("struct timespec[1]")
  if ffi.C.clock_gettime(1, ts) ~= 0 then error("clock_gettime failed") end
  return tonumber(ts[0].tv_sec) + tonumber(ts[0].tv_nsec) / 1e9
end
function done(summary, latency, requests)
  local finished = monotonic()
  local e = summary.errors
  local bins = #latency
  if bins > 100000 then error("latency histogram exceeds exporter limit") end
  io.write(string.format(
    'EULER_BENCH_JSON {"schema_version":1,"duration_us":%.0f,"requests":%.0f,' ..
    '"bytes":%.0f,"p50_us":%.3f,"p95_us":%.3f,"p99_us":%.3f,' ..
    '"mean_us":%.3f,"max_us":%.3f,"finished_monotonic":%.9f,' ..
    '"errors":{"connect":%.0f,"read":%.0f,"write":%.0f,"status":%.0f,"timeout":%.0f},' ..
    '"histogram_semantics":"wrk_reported_latency_distribution","histogram_us":[',
    summary.duration, summary.requests, summary.bytes,
    latency:percentile(50), latency:percentile(95), latency:percentile(99),
    latency.mean, latency.max, finished,
    e.connect, e.read, e.write, e.status, e.timeout))
  for index = 1, bins do
    local value, count = latency(index)
    if index > 1 then io.write(',') end
    io.write(string.format('[%.0f,%.0f]', value, count))
  end
  io.write(']}\n')
end
