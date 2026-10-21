-- Mixed CRUD: 55% read-one, 20% read-list, 15% create, 8% update, 2% delete.
-- Reads/updates use ids 1..8000; deletes use 8001..10000 so reads stay successful.
local get_headers = {}
local json_headers = { ["Content-Type"] = "application/json" }
local post_body = '{"name":"mixed-item","description":"mixed benchmark payload 0123456789 abcdefghijklmnopqrstuvwxyz","price":7.25}'
local put_body = '{"name":"mixed-updated","description":"mixed update payload 0123456789 abcdefghijklmnopqrstuvwxyz","price":8.5}'

math.randomseed(os.time() + math.floor(os.clock() * 1000000))

function request()
  local r = math.random(100)
  if r <= 55 then
    return wrk.format("GET", "/items/" .. math.random(1, 8000), get_headers)
  elseif r <= 75 then
    return wrk.format("GET", "/items?limit=20&offset=" .. math.random(0, 7000), get_headers)
  elseif r <= 90 then
    return wrk.format("POST", "/items", json_headers, post_body)
  elseif r <= 98 then
    return wrk.format("PUT", "/items/" .. math.random(1, 8000), json_headers, put_body)
  end
  return wrk.format("DELETE", "/items/" .. math.random(8001, 10000), get_headers)
end
