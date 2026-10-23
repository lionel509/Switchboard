// apple/shim.swift -- Anthropic /v1/messages over Apple's Foundation Models (#98).
//
// Localhost only (127.0.0.1). One-shot: every request gets a fresh session, the
// whole reply arrives at once (no token streaming), and an over-window prompt is
// refused, never truncated. Logs carry model, status, timing and token counts,
// never prompt or reply text.
//
// Build and run with apple/run.sh. Routes: GET /health, POST /v1/messages,
// POST /v1/messages/count_tokens.

import Foundation
import Network
import FoundationModels

let portArg = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "8788"
guard let port = UInt16(portArg) else {
    fatalError("apple shim: bad port \(portArg)")
}
let maxBody = 1_000_000

func logLine(_ s: String) {
    FileHandle.standardError.write(Data((s + "\n").utf8))
}

func nowISO() -> String {
    ISO8601DateFormatter().string(from: Date())
}

func jsonData(_ obj: Any) -> Data {
    (try? JSONSerialization.data(withJSONObject: obj, options: [])) ?? Data("{}".utf8)
}

func errorBody(_ type: String, _ message: String) -> Data {
    jsonData(["type": "error", "error": ["type": type, "message": message]])
}

// MARK: - HTTP framing

struct Request {
    let method: String
    let path: String        // query string stripped: the router sends ?beta=true
    let body: Data
}

enum Frame {
    case needMore
    case ready(Request)
    case reject(Int, String, String)   // status, error type, message
}

func frame(_ buf: Data) -> Frame {
    guard let end = buf.range(of: Data("\r\n\r\n".utf8)) else {
        return buf.count > 16_384 ? .reject(431, "invalid_request_error", "headers too large") : .needMore
    }
    let head = String(decoding: buf[0..<end.lowerBound], as: UTF8.self)
    let lines = head.components(separatedBy: "\r\n")
    let parts = (lines.first ?? "").split(separator: " ")
    guard parts.count >= 2 else {
        return .reject(400, "invalid_request_error", "bad request line")
    }
    var headers: [String: String] = [:]
    for line in lines.dropFirst() {
        if let colon = line.firstIndex(of: ":") {
            let key = line[..<colon].lowercased()
            headers[key] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
    }
    let method = String(parts[0])
    let path = String(parts[1]).components(separatedBy: "?")[0]
    var body = Data()
    if method == "POST" {
        guard let cl = headers["content-length"], let n = Int(cl) else {
            return .reject(411, "invalid_request_error", "POST needs a Content-Length header")
        }
        if n > maxBody {
            return .reject(413, "request_too_large", "request body is over 1 MB")
        }
        let start = end.upperBound
        if buf.count - start < n {
            return .needMore
        }
        body = buf.subdata(in: start..<(start + n))
    }
    return .ready(Request(method: method, path: path, body: body))
}

func httpHead(_ status: Int, type: String, length: Int?) -> Data {
    let reasons = [200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                   411: "Length Required", 413: "Payload Too Large", 429: "Too Many Requests",
                   431: "Request Header Fields Too Large", 500: "Internal Server Error",
                   503: "Service Unavailable"]
    var s = "HTTP/1.1 \(status) \(reasons[status] ?? "Error")\r\nContent-Type: \(type)\r\nConnection: close\r\n"
    if let n = length {
        s += "Content-Length: \(n)\r\n"
    }
    return Data((s + "\r\n").utf8)
}

// MARK: - Connections

final class Conn {
    let conn: NWConnection
    var buf = Data()

    init(_ c: NWConnection) {
        conn = c
    }

    func start() {
        conn.start(queue: .global())
        readMore()
    }

    func readMore() {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { data, _, complete, error in
            if let data = data {
                self.buf.append(data)
            }
            switch frame(self.buf) {
            case .needMore:
                if complete || error != nil {
                    self.conn.cancel()
                } else {
                    self.readMore()
                }
            case .reject(let status, let type, let message):
                self.send(httpHead(status, type: "application/json", length: errorBody(type, message).count)
                          + errorBody(type, message), close: true)
            case .ready(let req):
                self.route(req)
            }
        }
    }

    func send(_ data: Data, close: Bool) {
        conn.send(content: data, completion: .contentProcessed { _ in
            if close {
                self.conn.cancel()
            }
        })
    }

    func route(_ req: Request) {
        if req.method == "GET" && req.path == "/health" {
            let body = jsonData(["on-device": onDeviceState(), "pcc": pccState()])
            send(httpHead(200, type: "application/json", length: body.count) + body, close: true)
        } else if req.method == "POST" && (req.path == "/v1/messages" || req.path == "/v1/messages/count_tokens") {
            let count = req.path.hasSuffix("count_tokens")
            Task {
                await self.messages(req.body, count: count)
            }
        } else {
            let body = errorBody("not_found_error", "no route \(req.method) \(req.path)")
            send(httpHead(404, type: "application/json", length: body.count) + body, close: true)
        }
    }

    func messages(_ body: Data, count: Bool) async {
        let t0 = Date()
        let (model, out) = await handle(body, count: count)
        let ms = Int(Date().timeIntervalSince(t0) * 1000)
        logLine("\(nowISO()) model=\(model) status=\(out.status) ms=\(ms) in=\(out.inTok) out=\(out.outTok)")
        send(httpHead(out.status, type: out.sse ? "text/event-stream" : "application/json",
                      length: out.sse ? nil : out.body.count) + out.body, close: true)
    }
}

// MARK: - Request handling

struct Out {
    let status: Int
    let body: Data
    var sse = false
    var inTok = 0
    var outTok = 0
}

struct Refuse: Error {
    let status: Int
    let type: String
    let message: String
}

struct Parsed {
    let system: String?
    let prompt: String
    let maxTokens: Int?
    let temperature: Double?
    let stream: Bool
}

enum Target: String {
    case onDevice = "on-device"
    case pcc = "pcc"
}

// A prefixed id must carry its own backend: apple/pcc is not a model (#98).
// A bare "on-device" or "pcc" is what the router sends after its prefix is stripped.
func target(_ model: String) -> Target? {
    guard let slash = model.firstIndex(of: "/") else {
        return Target(rawValue: model)
    }
    let prefix = String(model[..<slash])
    let suffix = String(model[model.index(after: slash)...])
    switch (prefix, suffix) {
    case ("apple", "on-device"), ("apple-pcc", "pcc"):
        return Target(rawValue: suffix)
    default:
        return nil
    }
}

// Anthropic content is a string or an array of blocks. Only text is accepted:
// an image or tool block would be dropped silently, so it is refused by name.
func textOf(_ value: Any?, _ where_: String) throws -> String? {
    guard let value = value, !(value is NSNull) else {
        return nil
    }
    if let s = value as? String {
        return s
    }
    guard let blocks = value as? [[String: Any]] else {
        throw Refuse(status: 400, type: "invalid_request_error",
                     message: "\(where_) must be a string or an array of text blocks")
    }
    var parts: [String] = []
    for b in blocks {
        let kind = b["type"] as? String ?? "?"
        guard kind == "text" else {
            throw Refuse(status: 400, type: "invalid_request_error",
                         message: "apple models take text only (a \(kind) block in \(where_))")
        }
        parts.append(b["text"] as? String ?? "")
    }
    return parts.joined(separator: "\n\n")
}

func parse(_ obj: [String: Any]) throws -> Parsed {
    if let tools = obj["tools"] {
        guard let list = tools as? [Any] else {
            throw Refuse(status: 400, type: "invalid_request_error", message: "tools must be an array")
        }
        if !list.isEmpty {
            throw Refuse(status: 400, type: "invalid_request_error",
                         message: "apple models take no tools (\(list.count) sent): call with --tools \"\" --strict-mcp-config")
        }
    }
    let system = try textOf(obj["system"], "system")
    guard let messages = obj["messages"] as? [[String: Any]] else {
        throw Refuse(status: 400, type: "invalid_request_error", message: "messages must be an array")
    }
    guard messages.count == 1, messages[0]["role"] as? String == "user" else {
        throw Refuse(status: 400, type: "invalid_request_error",
                     message: "apple models are one-shot: send exactly one user message (got \(messages.count))")
    }
    let prompt = try textOf(messages[0]["content"], "the user message") ?? ""
    return Parsed(system: system,
                  prompt: prompt,
                  maxTokens: obj["max_tokens"] as? Int,
                  temperature: obj["temperature"] as? Double,
                  stream: obj["stream"] as? Bool ?? false)
}

func onDeviceState() -> String {
    switch SystemLanguageModel.default.availability {
    case .available:
        return "available"
    case .unavailable(let reason):
        return "unavailable: \(reason)"
    @unknown default:
        return "unknown"
    }
}

func pccState() -> String {
    switch PrivateCloudComputeLanguageModel().availability {
    case .available:
        return "available"
    case .unavailable(let reason):
        return "unavailable: \(reason)"
    @unknown default:
        return "unknown"
    }
}

// Token counts: the on-device model counts its own tokens; everything else
// (PCC, and count_tokens) uses the usual ~4 characters per token estimate.
func countText(_ model: Any, _ text: String) async -> Int {
    if let sm = model as? SystemLanguageModel {
        if let n = try? await sm.tokenCount(for: Prompt(text)) {
            return n
        }
    }
    return max(1, text.count / 4)
}

func countIn(_ model: Any, _ p: Parsed) async -> Int {
    var n = await countText(model, p.prompt)
    if let s = p.system, !s.isEmpty {
        if let sm = model as? SystemLanguageModel, let k = try? await sm.tokenCount(for: Instructions(s)) {
            n += k
        } else {
            n += max(1, s.count / 4)
        }
    }
    return n
}

func sseBody(model: String, text: String, inTok: Int, outTok: Int, stop: String) -> Data {
    func event(_ name: String, _ obj: [String: Any]) -> String {
        "event: \(name)\ndata: \(String(decoding: jsonData(obj), as: UTF8.self))\n\n"
    }
    let id = "msg_apple_" + UUID().uuidString
    let message: [String: Any] = ["id": id, "type": "message", "role": "assistant", "model": model,
                                  "content": [], "stop_reason": NSNull(), "stop_sequence": NSNull(),
                                  "usage": ["input_tokens": inTok, "output_tokens": 0]]
    let events = [
        event("message_start", ["type": "message_start", "message": message]),
        event("content_block_start", ["type": "content_block_start", "index": 0,
                                      "content_block": ["type": "text", "text": ""]]),
        event("content_block_delta", ["type": "content_block_delta", "index": 0,
                                      "delta": ["type": "text_delta", "text": text]]),
        event("content_block_stop", ["type": "content_block_stop", "index": 0]),
        event("message_delta", ["type": "message_delta",
                                "delta": ["stop_reason": stop, "stop_sequence": NSNull()],
                                "usage": ["output_tokens": outTok]]),
        event("message_stop", ["type": "message_stop"]),
    ]
    return Data(events.joined().utf8)
}

// Each request gets a fresh session, so nothing carries over between calls.
func generate<M: LanguageModel>(_ model: M, _ p: Parsed, name: String) async -> Out {
    let options = GenerationOptions(temperature: p.temperature, maximumResponseTokens: p.maxTokens)
    let session = LanguageModelSession(model: model, instructions: p.system)
    do {
        let reply = try await session.respond(to: Prompt(p.prompt), options: options)
        let text = reply.content
        let inTok = await countIn(model, p)
        let outTok = await countText(model, text)
        let stop = (p.maxTokens != nil && outTok >= p.maxTokens!) ? "max_tokens" : "end_turn"
        if p.stream {
            var out = Out(status: 200, body: sseBody(model: name, text: text, inTok: inTok, outTok: outTok, stop: stop))
            out.sse = true
            out.inTok = inTok
            out.outTok = outTok
            return out
        }
        let msg: [String: Any] = [
            "id": "msg_apple_" + UUID().uuidString, "type": "message", "role": "assistant",
            "model": name, "content": [["type": "text", "text": text]],
            "stop_reason": stop, "stop_sequence": NSNull(),
            "usage": ["input_tokens": inTok, "output_tokens": outTok],
        ]
        return Out(status: 200, body: jsonData(msg), inTok: inTok, outTok: outTok)
    } catch {
        return mapError(error)
    }
}

func fail(_ status: Int, _ type: String, _ message: String) -> Out {
    Out(status: status, body: errorBody(type, message))
}

// PCC wraps ModelManagerError 1046 (an unsigned binary) inside its own
// LanguageModelError, so walk the underlying errors rather than the outer domain.
func isUnsignedPCC(_ e: NSError) -> Bool {
    if e.domain.contains("ModelManager") && e.code == 1046 {
        return true
    }
    var inner: [NSError] = []
    if let u = e.userInfo[NSUnderlyingErrorKey] as? NSError {
        inner.append(u)
    }
    if let many = e.userInfo[NSMultipleUnderlyingErrorsKey] as? [NSError] {
        inner += many
    }
    return inner.contains(where: isUnsignedPCC)
}

// Never 401: the router would try to renew a key that does not exist (router.py).
func mapError(_ error: Error) -> Out {
    if let e = error as? LanguageModelError {
        switch e {
        case .contextSizeExceeded(let c):
            return fail(400, "invalid_request_error",
                        "prompt is \(c.tokenCount) tokens; the model holds \(c.contextSize) "
                        + "(instructions + prompt + reply). Nothing was truncated.")
        case .guardrailViolation, .refusal:
            return fail(400, "invalid_request_error", "Apple's guardrails refused this prompt")
        case .unsupportedLanguageOrLocale:
            return fail(400, "invalid_request_error", "Apple's model does not support this language or locale")
        case .rateLimited:
            return fail(429, "rate_limit_error", "Apple's model is rate limited; retry shortly")
        default:
            break
        }
    }
    if let e = error as? PrivateCloudComputeLanguageModel.Error, case .quotaLimitReached = e {
        return fail(429, "rate_limit_error", "Private Cloud Compute quota reached; retry later")
    }
    if isUnsignedPCC(error as NSError) {
        return fail(403, "permission_error",
                    "Private Cloud Compute refused this unsigned binary (ModelManagerError 1046): "
                    + "PCC needs a signed app with Apple's entitlement; use apple/on-device")
    }
    return fail(500, "api_error", String(describing: error))
}

// Returns the model string for the log line, and the outcome.
extension Conn {
    func handle(_ body: Data, count: Bool) async -> (String, Out) {
        guard let obj = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] else {
            return ("?", fail(400, "invalid_request_error", "body is not a JSON object"))
        }
        let model = obj["model"] as? String ?? ""
        guard let t = target(model) else {
            return (model, fail(404, "not_found_error",
                                "model \(model): this shim serves on-device, pcc"))
        }
        let parsed: Parsed
        do {
            parsed = try parse(obj)
        } catch let r as Refuse {
            return (model, fail(r.status, r.type, r.message))
        } catch {
            return (model, fail(400, "invalid_request_error", String(describing: error)))
        }
        if count {
            let n = max(1, (parsed.system ?? "").count / 4 + parsed.prompt.count / 4)
            var out = Out(status: 200, body: jsonData(["input_tokens": n]))
            out.inTok = n
            return (model, out)
        }
        switch t {
        case .onDevice:
            let m = SystemLanguageModel.default
            if case .available = m.availability {} else {
                return (model, fail(503, "api_error", "on-device model unavailable: \(onDeviceState())"))
            }
            return (model, await generate(m, parsed, name: model))
        case .pcc:
            let m = PrivateCloudComputeLanguageModel()
            if case .available = m.availability {} else {
                return (model, fail(503, "api_error", "Private Cloud Compute unavailable: \(pccState())"))
            }
            return (model, await generate(m, parsed, name: model))
        }
    }
}

// MARK: - Listener

let params = NWParameters.tcp
params.allowLocalEndpointReuse = true
params.requiredLocalEndpoint = NWEndpoint.hostPort(host: NWEndpoint.Host("127.0.0.1"),
                                                   port: NWEndpoint.Port(rawValue: port)!)
let listener = try NWListener(using: params)
listener.newConnectionHandler = { c in
    Conn(c).start()
}
listener.stateUpdateHandler = { state in
    if case .failed(let error) = state {
        logLine("apple shim: listener failed: \(error)")
        exit(1)
    }
}
listener.start(queue: .global())
logLine("apple shim on 127.0.0.1:\(port) (on-device, pcc)")
dispatchMain()
