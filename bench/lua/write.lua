-- Constant create workload (POST /items)
local body = '{"name":"bench-item","description":"benchmark payload 0123456789 abcdefghijklmnopqrstuvwxyz","price":12.34}'
local headers = { ["Content-Type"] = "application/json" }

function request()
  return wrk.format("POST", "/items", headers, body)
end
