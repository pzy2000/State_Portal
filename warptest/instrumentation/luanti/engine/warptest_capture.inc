// WARPTEST-CAPTURE-BEGIN (installed by warptest.luanti_runtime.ensure_luanti_instrumentation)
// Config-gated screenshot capture for the WarpTest visual oracle. Two modes:
//   * One-shot (legacy): after `warptest_screenshot_frame` rendered frames, write the
//     framebuffer to `warptest_screenshot_path` and optionally request shutdown.
//   * Persistent on-demand (warm session): when `warptest_screenshot_request_path`
//     is set, poll that file each frame. Its first line is an opaque token and its
//     second line a target path; when the token changes, capture the current frame to
//     the path, write the token to `warptest_screenshot_done_path`, and keep running.
// Both reuse the same IVideoDriver::createScreenShot path as Client::makeScreenshot.
#include <algorithm>
#include <fstream>
#include <cstdio>
#include <vector>
#include <json/json.h>
#include "convert_json.h"
#include "util/string.h"

static int warptest_policy_frame_id = -1;
static bool warptest_policy_frame_consumed = true;
static int warptest_background_sequence = 0;
static std::string warptest_last_batch_id;
static Json::Value warptest_last_batch_receipt;
static bool warptest_background_input_pending = false;
static int warptest_background_input_frames = 0;
static u32 warptest_background_input_release_at = 0;
static std::string warptest_pending_response_path;
static Json::Value warptest_pending_batch_receipt;

static bool warptest_write_json_atomic(const std::string &path, const Json::Value &value)
{
	const std::string temporary = path + ".tmp";
	std::ofstream output(temporary.c_str(), std::ios::trunc);
	if (!output.good())
		return false;
	fastWriteJson(value, output);
	output.close();
	std::remove(path.c_str());
	return std::rename(temporary.c_str(), path.c_str()) == 0;
}

static EKEY_CODE warptest_key_code(const std::string &raw)
{
	std::string key = lowercase(raw);
	if (key.size() == 1 && key[0] >= 'a' && key[0] <= 'z')
		return static_cast<EKEY_CODE>(KEY_KEY_A + key[0] - 'a');
	if (key.size() == 1 && key[0] >= '0' && key[0] <= '9')
		return static_cast<EKEY_CODE>(KEY_KEY_0 + key[0] - '0');
	if (key == "enter" || key == "return") return KEY_RETURN;
	if (key == "escape" || key == "esc") return KEY_ESCAPE;
	if (key == "backspace") return KEY_BACK;
	if (key == "delete") return KEY_DELETE;
	if (key == "tab") return KEY_TAB;
	if (key == "space") return KEY_SPACE;
	if (key == "left") return KEY_LEFT;
	if (key == "right") return KEY_RIGHT;
	if (key == "up") return KEY_UP;
	if (key == "down") return KEY_DOWN;
	if (key == "home") return KEY_HOME;
	if (key == "end") return KEY_END;
	if (key == "pageup") return KEY_PRIOR;
	if (key == "pagedown") return KEY_NEXT;
	if (key == "/" || key == "slash") return KEY_OEM_2;
	if (key == "\\" || key == "backslash") return KEY_OEM_5;
	if (key == "," || key == "comma") return KEY_COMMA;
	if (key == "." || key == "period") return KEY_PERIOD;
	if (key == "-" || key == "minus") return KEY_MINUS;
	if (key == "=" || key == "plus") return KEY_PLUS;
	if (key == ";" || key == "semicolon") return KEY_OEM_1;
	if (key == "'" || key == "apostrophe") return KEY_OEM_7;
	if (key == "[" || key == "leftbracket") return KEY_OEM_4;
	if (key == "]" || key == "rightbracket") return KEY_OEM_6;
	if (key == "`" || key == "grave") return KEY_OEM_3;
	if (key.size() >= 2 && key[0] == 'f') {
		int number = atoi(key.c_str() + 1);
		if (number >= 1 && number <= 24)
			return static_cast<EKEY_CODE>(KEY_F1 + number - 1);
	}
	return KEY_UNKNOWN;
}

static u32 warptest_system_key_code(const std::string &raw)
{
	const std::string key = lowercase(raw);
	if (key.size() == 1 && key[0] >= 'a' && key[0] <= 'z')
		return 4 + key[0] - 'a';
	if (key.size() == 1 && key[0] >= '1' && key[0] <= '9')
		return 30 + key[0] - '1';
	if (key == "0") return 39;
	if (key == "enter" || key == "return") return 40;
	if (key == "escape" || key == "esc") return 41;
	if (key == "backspace") return 42;
	if (key == "tab") return 43;
	if (key == "space") return 44;
	if (key == "-" || key == "minus") return 45;
	if (key == "=" || key == "plus") return 46;
	if (key == "[" || key == "leftbracket") return 47;
	if (key == "]" || key == "rightbracket") return 48;
	if (key == "\\" || key == "backslash") return 49;
	if (key == ";" || key == "semicolon") return 51;
	if (key == "'" || key == "apostrophe") return 52;
	if (key == "`" || key == "grave") return 53;
	if (key == "," || key == "comma") return 54;
	if (key == "." || key == "period") return 55;
	if (key == "/" || key == "slash") return 56;
	if (key.size() >= 2 && key[0] == 'f') {
		int number = atoi(key.c_str() + 1);
		if (number >= 1 && number <= 12)
			return 57 + number;
	}
	if (key == "right") return 79;
	if (key == "left") return 80;
	if (key == "down") return 81;
	if (key == "up") return 82;
	return 0;
}

static wchar_t warptest_key_character(const std::string &raw)
{
	const std::string key = lowercase(raw);
	return key.size() == 1 ? static_cast<unsigned char>(key[0]) : 0;
}

struct WarptestModifiers {
	bool shift = false;
	bool control = false;
	bool alt = false;
	bool meta = false;
};

struct WarptestPendingKeyRelease {
	EKEY_CODE key = KEY_UNKNOWN;
	u32 system_key_code = 0;
	WarptestModifiers modifiers;
};

static std::vector<WarptestPendingKeyRelease> warptest_pending_key_releases;

static bool warptest_background_input_is_pending()
{
	return warptest_background_input_pending;
}

static bool warptest_modifiers(const Json::Value &value, WarptestModifiers *out)
{
	if (!value.isNull() && !value.isArray())
		return false;
	for (const auto &entry : value) {
		const std::string name = lowercase(entry.asString());
		if (name == "shift") out->shift = true;
		else if (name == "ctrl" || name == "control") out->control = true;
		else if (name == "alt" || name == "option") out->alt = true;
		else if (name == "meta" || name == "command" || name == "cmd") out->meta = true;
		else return false;
	}
	return true;
}

static void warptest_key_event(IrrlichtDevice *device, EKEY_CODE key, wchar_t character,
		bool pressed, const WarptestModifiers &modifiers, u32 system_key_code = 0)
{
	SEvent event{};
	event.EventType = EET_KEY_INPUT_EVENT;
	event.KeyInput.Key = key;
	event.KeyInput.Char = character;
	event.KeyInput.SystemKeyCode = system_key_code;
	event.KeyInput.PressedDown = pressed;
	event.KeyInput.Shift = modifiers.shift;
	event.KeyInput.Control = modifiers.control;
	device->postEventFromUser(event);
}

static int warptest_tap_key(IrrlichtDevice *device, EKEY_CODE key, wchar_t character,
		const WarptestModifiers &modifiers)
{
	if (key == KEY_UNKNOWN && character == 0)
		return -1;
	if (modifiers.alt) warptest_key_event(device, KEY_LMENU, 0, true, modifiers);
	if (modifiers.meta) warptest_key_event(device, KEY_LWIN, 0, true, modifiers);
	warptest_key_event(device, key, character, true, modifiers);
	warptest_key_event(device, key, 0, false, modifiers);
	if (modifiers.meta) warptest_key_event(device, KEY_LWIN, 0, false, modifiers);
	if (modifiers.alt) warptest_key_event(device, KEY_LMENU, 0, false, modifiers);
	return 2 + (modifiers.alt ? 2 : 0) + (modifiers.meta ? 2 : 0);
}

static int warptest_hold_key(IrrlichtDevice *device, EKEY_CODE key,
		wchar_t character, u32 system_key_code,
		const WarptestModifiers &modifiers, u32 hold_ms)
{
	if (key == KEY_UNKNOWN)
		return -1;
	if (modifiers.alt) warptest_key_event(device, KEY_LMENU, 0, true, modifiers);
	if (modifiers.meta) warptest_key_event(device, KEY_LWIN, 0, true, modifiers);
	warptest_key_event(device, key, character, true, modifiers, system_key_code);
	warptest_pending_key_releases.push_back({key, system_key_code, modifiers});
	const u32 now = device->getTimer()->getTime();
	warptest_background_input_release_at = std::max(
			warptest_background_input_release_at, now + hold_ms);
	return 2 + (modifiers.alt ? 2 : 0) + (modifiers.meta ? 2 : 0);
}

static void warptest_release_pending_keys(IrrlichtDevice *device)
{
	for (auto it = warptest_pending_key_releases.rbegin();
			it != warptest_pending_key_releases.rend(); ++it) {
		warptest_key_event(device, it->key, 0, false, it->modifiers,
				it->system_key_code);
		if (it->modifiers.meta)
			warptest_key_event(device, KEY_LWIN, 0, false, it->modifiers);
		if (it->modifiers.alt)
			warptest_key_event(device, KEY_LMENU, 0, false, it->modifiers);
	}
	warptest_pending_key_releases.clear();
}

static int warptest_mouse_event(IrrlichtDevice *device, EMOUSE_INPUT_EVENT type,
		int x, int y, int button, u32 states, const WarptestModifiers &modifiers, float wheel = 0.0f)
{
	if (x < 0 || x >= 1280 || y < 0 || y >= 720)
		return -1;
	SEvent event{};
	event.EventType = EET_MOUSE_INPUT_EVENT;
	event.MouseInput.X = x;
	event.MouseInput.Y = y;
	event.MouseInput.Wheel = wheel;
	event.MouseInput.Button = button;
	event.MouseInput.ButtonStates = states;
	event.MouseInput.Shift = modifiers.shift;
	event.MouseInput.Control = modifiers.control;
	event.MouseInput.Simulated = true;
	event.MouseInput.Event = type;
	if (type == EMIE_MOUSE_MOVED && device->getCursorControl())
		device->getCursorControl()->setPosition(x, y);
	device->postEventFromUser(event);
	return 1;
}

static int warptest_dispatch_action(IrrlichtDevice *device, const Json::Value &action)
{
	const std::string kind = action.get("kind", "").asString();
	if (kind == "done" || kind == "fail") return 0;
	if (kind == "wait") {
		const double seconds = action.get("seconds", 0.0).asDouble();
		if (seconds < 0.0 || seconds > 5.0)
			return -1;
		const u32 now = device->getTimer()->getTime();
		warptest_background_input_release_at = std::max(
				warptest_background_input_release_at,
				now + static_cast<u32>(seconds * 1000.0));
		return 0;
	}
	WarptestModifiers modifiers;
	if (!warptest_modifiers(action["modifiers"], &modifiers)) return -1;
	if (kind == "key") {
		const std::string raw_key = lowercase(action.get("key", "").asString());
		u32 hold_ms = 0;
		if (action.isMember("duration") && !action["duration"].isNull()) {
			const double duration = action["duration"].asDouble();
			if (duration < 0.0 || duration > 5.0) return -1;
			hold_ms = static_cast<u32>(duration * 1000.0);
		} else if (modifiers.control || modifiers.alt || modifiers.meta) {
			hold_ms = 0;
		} else if (raw_key == "w" || raw_key == "a" || raw_key == "s" ||
				raw_key == "d" || raw_key == "space") {
			hold_ms = 120;
		}
		return warptest_hold_key(device, warptest_key_code(raw_key),
				warptest_key_character(raw_key), warptest_system_key_code(raw_key),
				modifiers, hold_ms);
	}
	if (kind == "type") {
		int count = 0;
		if (!action["x"].isNull() && !action["y"].isNull()) {
			Json::Value click = action;
			click["kind"] = "click";
			click["clicks"] = 1;
			click["button"] = "left";
			int result = warptest_dispatch_action(device, click);
			if (result < 0) return -1;
			count += result;
		}
		if (action.get("overwrite", false).asBool()) {
			WarptestModifiers select_all;
			select_all.meta = true;
			count += warptest_tap_key(device, KEY_KEY_A, L'a', select_all);
			count += warptest_tap_key(device, KEY_BACK, 0, WarptestModifiers{});
		}
		const std::string text = action.get("text", "").asString();
		if (text.size() > 16384) return -1;
		core::stringw wide = utf8_to_wide(text).c_str();
		SEvent text_event{};
		text_event.EventType = EET_STRING_INPUT_EVENT;
		text_event.StringInput.Str = &wide;
		device->postEventFromUser(text_event);
		count++;
		if (action.get("enter", false).asBool())
			count += warptest_tap_key(device, KEY_RETURN, 0, WarptestModifiers{});
		return count;
	}
	if (kind == "click") {
		const std::string button_name = action.get("button", "left").asString();
		const int button = button_name == "right" ? SDL_BUTTON_RIGHT :
				button_name == "middle" ? SDL_BUTTON_MIDDLE : SDL_BUTTON_LEFT;
		const auto down = button == SDL_BUTTON_RIGHT ? EMIE_RMOUSE_PRESSED_DOWN :
				button == SDL_BUTTON_MIDDLE ? EMIE_MMOUSE_PRESSED_DOWN : EMIE_LMOUSE_PRESSED_DOWN;
		const auto up = button == SDL_BUTTON_RIGHT ? EMIE_RMOUSE_LEFT_UP :
				button == SDL_BUTTON_MIDDLE ? EMIE_MMOUSE_LEFT_UP : EMIE_LMOUSE_LEFT_UP;
		const int clicks = action.get("clicks", 1).asInt();
		if (clicks < 1 || clicks > 3) return -1;
		int count = warptest_mouse_event(device, EMIE_MOUSE_MOVED, action["x"].asInt(), action["y"].asInt(), 0, 0, modifiers);
		for (int i = 0; i < clicks; ++i) {
			if (warptest_mouse_event(device, down, action["x"].asInt(), action["y"].asInt(), button,
					SDL_BUTTON_MASK(button), modifiers) < 0) return -1;
			warptest_mouse_event(device, up, action["x"].asInt(), action["y"].asInt(), button, 0, modifiers);
			count += 2;
		}
		return count;
	}
	if (kind == "scroll") {
		const int dx = action.get("dx", 0).asInt();
		const int dy = action.get("dy", 0).asInt();
		return warptest_mouse_event(device, EMIE_MOUSE_WHEEL, action["x"].asInt(), action["y"].asInt(),
				0, 0, modifiers, static_cast<float>(dy != 0 ? dy : dx));
	}
	if (kind == "drag") {
		const double duration = action.get("duration", 0.0).asDouble();
		if (duration < 0.0 || duration > 5.0) return -1;
		const int x = action["x"].asInt(), y = action["y"].asInt();
		const int x2 = action["x2"].asInt(), y2 = action["y2"].asInt();
		if (warptest_mouse_event(device, EMIE_MOUSE_MOVED, x, y, 0, 0, modifiers) < 0) return -1;
		if (warptest_mouse_event(device, EMIE_LMOUSE_PRESSED_DOWN, x, y, SDL_BUTTON_LEFT,
				SDL_BUTTON_MASK(SDL_BUTTON_LEFT), modifiers) < 0) return -1;
		for (int step = 1; step <= 6; ++step) {
			int px = x + (x2 - x) * step / 6;
			int py = y + (y2 - y) * step / 6;
			if (warptest_mouse_event(device, EMIE_MOUSE_MOVED, px, py, 0,
					SDL_BUTTON_MASK(SDL_BUTTON_LEFT), modifiers) < 0) return -1;
		}
		warptest_mouse_event(device, EMIE_LMOUSE_LEFT_UP, x2, y2, SDL_BUTTON_LEFT, 0, modifiers);
		return 9;
	}
	return -1;
}
static bool warptest_write_screenshot(video::IVideoDriver *driver, const std::string &out_path)
{
	video::IImage *raw_image = driver->createScreenShot();
	if (!raw_image) {
		errorstream << "[warptest] createScreenShot returned null" << std::endl;
		return false;
	}
	bool ok = false;
	video::IImage *image = driver->createImage(video::ECF_R8G8B8, raw_image->getDimension());
	if (image) {
		raw_image->copyTo(image);
		ok = driver->writeImageToFile(image, out_path.c_str(), 85);
		if (ok)
			infostream << "[warptest] captured screenshot to " << out_path << std::endl;
		else
			errorstream << "[warptest] failed to write screenshot to " << out_path << std::endl;
		image->drop();
	}
	raw_image->drop();
	return ok;
}

static void warptest_finish_background_input_frame(IrrlichtDevice *device)
{
	if (!warptest_background_input_pending || !device)
		return;
	warptest_background_input_frames++;
	const u32 now = device->getTimer()->getTime();
	if (warptest_background_input_frames < 1 || now < warptest_background_input_release_at)
		return;
	warptest_release_pending_keys(device);
	warptest_pending_batch_receipt["status"] = "success";
	warptest_pending_batch_receipt["accepted"] = true;
	warptest_pending_batch_receipt["consumed"] = true;
	warptest_pending_batch_receipt["consumed_frame_count"] = warptest_background_input_frames;
	warptest_pending_batch_receipt["consumer"] = "luanti_game_loop_v1";
	warptest_last_batch_id = warptest_pending_batch_receipt.get("batch_id", "").asString();
	warptest_last_batch_receipt = warptest_pending_batch_receipt;
	warptest_write_json_atomic(warptest_pending_response_path, warptest_pending_batch_receipt);
	warptest_pending_batch_receipt = Json::Value();
	warptest_pending_response_path.clear();
	warptest_background_input_pending = false;
	warptest_background_input_frames = 0;
	warptest_background_input_release_at = 0;
}

static void warptest_maybe_background_request(IrrlichtDevice *device, video::IVideoDriver *driver)
{
	if (!device || !driver || !g_settings->exists("warptest_background_request_path"))
		return;
	if (warptest_background_input_pending)
		return;
	const std::string request_path = g_settings->get("warptest_background_request_path");
	const std::string response_path = g_settings->get("warptest_background_response_path");
	const std::string session_id = g_settings->get("warptest_background_session_id");
	const std::string root = g_settings->get("warptest_background_root");
	if (request_path.empty() || response_path.empty() || session_id.empty() || root.empty())
		return;
	std::ifstream input(request_path.c_str());
	if (!input.good())
		return;
	Json::CharReaderBuilder builder;
	Json::Value request;
	std::string errors;
	if (!Json::parseFromStream(builder, input, &request, &errors))
		return;
	const int sequence = request.get("sequence", -1).asInt();
	if (sequence <= warptest_background_sequence)
		return;
	Json::Value response(Json::objectValue);
	response["schema_version"] = 1;
	response["sequence"] = sequence;
	response["session_id"] = session_id;
	response["operation"] = request.get("operation", "").asString();
	response["status"] = "rejected";
	response["accepted"] = false;
	response["event_count"] = 0;
	response["consumed"] = false;
	response["resulting_frame_id"] = Json::nullValue;
	response["error"] = "";
	if (request.get("session_id", "").asString() != session_id) {
		response["error"] = "session nonce mismatch";
	} else if (sequence != warptest_background_sequence + 1) {
		response["error"] = "request sequence gap";
	} else if (response["operation"].asString() == "capture") {
		const std::string output_path = request.get("output_path", "").asString();
		const std::string prefix = root + "/";
		if (output_path.compare(0, prefix.size(), prefix) != 0 || output_path.find("..") != std::string::npos) {
			response["error"] = "capture path escapes session root";
		} else if (!warptest_write_screenshot(driver, output_path)) {
			response["error"] = "framebuffer capture failed";
		} else {
			warptest_policy_frame_id++;
			warptest_policy_frame_consumed = false;
			response["status"] = "success";
			response["accepted"] = true;
			response["frame_id"] = warptest_policy_frame_id;
			response["width"] = 1280;
			response["height"] = 720;
			response["capture_backend"] = "luanti_irrlicht_create_screenshot_v1";
			response["coordinate_space"] = "frame_pixels_top_left_v1";
		}
	} else if (response["operation"].asString() == "input_batch") {
		const int frame_id = request.get("frame_id", -1).asInt();
		const Json::Value actions = request["actions"];
		const std::string batch_id = request.get("batch_id", "").asString();
		if (!batch_id.empty() && batch_id == warptest_last_batch_id) {
			response = warptest_last_batch_receipt;
			response["sequence"] = sequence;
		} else if (frame_id != warptest_policy_frame_id || warptest_policy_frame_consumed) {
			response["error"] = "input batch references a stale or consumed frame";
		} else if (!actions.isArray() || actions.empty() || actions.size() > 64) {
			response["error"] = "input batch action count is outside 1..64";
		} else {
			int event_count = 0;
			for (const auto &action : actions) {
				int count = warptest_dispatch_action(device, action);
				if (count < 0) {
					warptest_release_pending_keys(device);
					warptest_background_input_release_at = 0;
					response["error"] = "invalid input action";
					event_count = -1;
					break;
				}
				event_count += count;
			}
			if (event_count >= 0) {
				warptest_policy_frame_consumed = true;
				response["status"] = "pending";
				response["batch_id"] = batch_id;
				response["event_count"] = event_count;
				response["resulting_frame_id"] = frame_id + 1;
				response["input_backend"] = "luanti_irrlicht_sevent_v1";
				warptest_background_input_pending = true;
				warptest_background_input_frames = 0;
				warptest_pending_response_path = response_path;
				warptest_pending_batch_receipt = response;
			}
		}
	} else {
		response["error"] = "unsupported background operation";
	}
	warptest_background_sequence = sequence;
	if (!warptest_background_input_pending)
		warptest_write_json_atomic(response_path, response);
}
static void warptest_maybe_capture(video::IVideoDriver *driver)
{
	if (!driver)
		return;

	// Persistent on-demand capture for the warm StateSession.
	if (g_settings->exists("warptest_screenshot_request_path")) {
		static std::string warptest_last_token;
		const std::string req_path = g_settings->get("warptest_screenshot_request_path");
		if (req_path.empty())
			return;
		std::ifstream req(req_path.c_str());
		if (!req.good())
			return;
		std::string token, out_path;
		std::getline(req, token);
		std::getline(req, out_path);
		req.close();
		if (token.empty() || out_path.empty() || token == warptest_last_token)
			return;
		const bool ok = warptest_write_screenshot(driver, out_path);
		if (!ok)
			return;  // leave token unconsumed so a later frame can retry this request
		warptest_last_token = token;
		const std::string done_path = g_settings->exists("warptest_screenshot_done_path")
				? g_settings->get("warptest_screenshot_done_path") : "";
		if (!done_path.empty()) {
			std::ofstream done(done_path.c_str(), std::ios::trunc);
			if (done.good()) {
				done << token << " ok" << std::endl;
				done.close();
			}
		}
		return;
	}

	// One-shot frame-based capture (legacy).
	static int warptest_frame = 0;
	static bool warptest_done = false;
	if (warptest_done || !g_settings->exists("warptest_screenshot_path"))
		return;
	const std::string warptest_path = g_settings->get("warptest_screenshot_path");
	if (warptest_path.empty())
		return;
	const int target_frame = g_settings->exists("warptest_screenshot_frame")
			? g_settings->getS32("warptest_screenshot_frame") : 120;
	if (++warptest_frame < (target_frame > 0 ? target_frame : 1))
		return;
	warptest_done = true;
	warptest_write_screenshot(driver, warptest_path);

	const bool exit_after = g_settings->exists("warptest_exit_after_screenshot")
			&& g_settings->getBool("warptest_exit_after_screenshot");
	if (exit_after && g_gamecallback)
		g_gamecallback->shutdown_requested = true;
}
// WARPTEST-CAPTURE-END
