-- Drive the real WarpTest worldmod under a stub engine API.
--
-- The point of the stub is to make `math.round` swappable: Luanti's engine
-- provides it, and the PR #14757 defect is exactly its half-way behaviour.  With
-- the nearest-even implementation both boundary values round to zero; with the
-- pre-fix floor/ceil implementation they round away from zero.  Everything else
-- here is the minimum surface the mod touches while it loads, while it serves a
-- chat command, and while it serves one persistent-loop request.
--
-- Usage: lua warptest_round_half_harness.lua <init.lua> <nearest_even|floor_ceil>

local init_path = assert(arg[1], "worldmod init.lua path is required")
local rounding = assert(arg[2], "rounding mode is required")
local request_path = assert(arg[3], "request file path is required")

if rounding == "nearest_even" then
	math.round = function(value)
		local floor = math.floor(value)
		local diff = value - floor
		if diff > 0.5 then
			return floor + 1
		end
		if diff < 0.5 then
			return floor
		end
		if floor % 2 == 0 then
			return floor
		end
		return floor + 1
	end
elseif rounding == "floor_ceil" then
	math.round = function(value)
		if value < 0 then
			return math.ceil(value - 0.5)
		end
		return math.floor(value + 0.5)
	end
else
	error("unknown rounding mode: " .. tostring(rounding))
end

local function pos_key(pos)
	return tostring(pos.x) .. "," .. tostring(pos.y) .. "," .. tostring(pos.z)
end

local nodes = {}
local storage_values = {}
local chatcommands = {}
local globalsteps = {}
local mods_loaded = {}
local afters = {}
local request = nil

local settings = {
	warptest_request_path = request_path,
	warptest_persistent = "true",
	name = "singleplayer",
}

core = {
	registered_chatcommands = chatcommands,
	get_worldpath = function()
		return "/tmp/warptest-round-half"
	end,
	settings = {
		get = function(_, key)
			return settings[key]
		end,
		get_bool = function(_, key, default)
			local value = settings[key]
			if value == nil then
				return default
			end
			return value == "true"
		end,
	},
	get_mod_storage = function()
		return {
			set_string = function(_, key, value)
				storage_values[key] = value
			end,
			get_string = function(_, key)
				return storage_values[key] or ""
			end,
		}
	end,
	log = function() end,
	register_chatcommand = function(name, definition)
		chatcommands[name] = definition
	end,
	register_globalstep = function(callback)
		globalsteps[#globalsteps + 1] = callback
	end,
	register_on_mods_loaded = function(callback)
		mods_loaded[#mods_loaded + 1] = callback
	end,
	register_on_joinplayer = function() end,
	after = function(_, callback)
		afters[#afters + 1] = callback
	end,
	set_node = function(pos, node)
		nodes[pos_key(pos)] = node.name
	end,
	get_node = function(pos)
		return { name = nodes[pos_key(pos)] or "air" }
	end,
	get_connected_players = function()
		return {}
	end,
	get_player_by_name = function()
		return nil
	end,
	registered_nodes = {
		["basenodes:cobble"] = {},
		["basenodes:dirt_with_grass"] = {},
		["basenodes:desert_sand"] = {},
	},
	parse_json = function()
		return request
	end,
	write_json = function()
		return "{}"
	end,
	safe_file_write = function()
		return true
	end,
}
minetest = core

vector = {
	new = function(value)
		return { x = value.x, y = value.y, z = value.z }
	end,
	add = function(left, right)
		return { x = left.x + right.x, y = left.y + right.y, z = left.z + right.z }
	end,
	check = function(value)
		return type(value) == "table" and value.x ~= nil
	end,
	from_string = function()
		return nil
	end,
	equals = function(left, right)
		return left.x == right.x and left.y == right.y and left.z == right.z
	end,
}

local marker = { x = 0, y = 1, z = 0 }
nodes[pos_key(marker)] = "basenodes:cobble"

assert(loadfile(init_path))()

-- Phase one: the public GUI chat command.
local command = assert(chatcommands["warp_round_half"], "chat command was not registered")
local ok, message = command.func("singleplayer", "")
print("CHAT_OK=" .. tostring(ok))
print("CHAT_MESSAGE=" .. tostring(message))
print("CHAT_MARKER=" .. tostring(nodes[pos_key(marker)]))
print("CHAT_STORAGE=" .. tostring(storage_values["lazarus_round_half_boundary"]))

-- Phase two: the semantic action through the persistent request loop.
nodes[pos_key(marker)] = "basenodes:cobble"
for _, callback in ipairs(mods_loaded) do
	callback()
end
for _, callback in ipairs(afters) do
	callback()
end
-- read_request only needs non-empty bytes: core.parse_json is stubbed above.
local handle = assert(io.open(request_path, "w"))
handle:write('{"seq": 1}')
handle:close()
request = {
	seq = 1,
	skip_synthesis = true,
	spec = {
		target = { kind = "world_loaded" },
		actions = {
			{ type = "luanti_round_half_boundary", marker_pos = marker },
		},
		assertions = {
			{ type = "luanti_node_is", pos = marker, expected = "basenodes:dirt_with_grass" },
		},
	},
}
assert(#globalsteps > 0, "persistent loop was not registered")
for _, callback in ipairs(globalsteps) do
	callback(1.0)
end
print("ACTION_MARKER=" .. tostring(nodes[pos_key(marker)]))
print("ACTION_STORAGE=" .. tostring(storage_values["lazarus_round_half_boundary"]))
