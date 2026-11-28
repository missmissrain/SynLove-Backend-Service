"use strict";
/**
 * 配置加载：读 JSON、补默认值、展开 ${ENV}、把相对路径按项目根解析。
 *
 * 负责的功能：
 * - `project_root()`：项目根目录（兼容 tsx 直跑 src 与编译后跑 dist 两种形态）；
 * - `read_json_file`：容错读 JSON（带 BOM 也认，坏文件抛明确错误而不是静默用默认值）；
 * - `expand_env`：递归把字符串里的 `${VAR}` 换成环境变量，缺失时保留 `${VAR}` 并告警；
 * - `load_hub_config` / `load_node_config`：产出带默认值的强类型配置对象；
 * - 环境变量覆盖：`RELAY_TOKEN`、`RELAY_PORT`、`RELAY_HOST`、`RELAY_HUB_URL`（部署时最常改的几个）。
 *
 * 注册参数（两个加载函数）：
 *   file  配置文件路径，默认分别是 config/hub.json、config/node.json；
 *         传 null 表示完全不读文件，只用默认值 + 环境变量（冒烟测试就这么用）。
 */
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.DEFAULT_TOKEN = void 0;
exports.project_root = project_root;
exports.read_json_file = read_json_file;
exports.resolve_path = resolve_path;
exports.expand_env = expand_env;
exports.load_hub_config = load_hub_config;
exports.load_node_config = load_node_config;
const node_fs_1 = __importDefault(require("node:fs"));
const node_os_1 = __importDefault(require("node:os"));
const node_path_1 = __importDefault(require("node:path"));
/**
 * 项目根目录。
 * 依据：本文件位于 <root>/src/common/ 或 <root>/dist/common/，往上两级即根。
 */
function project_root() {
    return node_path_1.default.resolve(__dirname, '..', '..');
}
/**
 * 读 JSON 文件（容错 BOM；文件不存在或语法错误都抛带路径的明确错误）。
 * 参数 file：绝对路径。
 */
function read_json_file(file) {
    const raw = node_fs_1.default.readFileSync(file, 'utf8').replace(/^\uFEFF/, '');
    try {
        return JSON.parse(raw);
    }
    catch (error) {
        throw new Error(`配置文件不是合法 JSON：${file}（${error.message}）`);
    }
}
/**
 * 把配置里的相对路径按项目根解析（绝对路径原样返回）。
 * 参数 base：基准目录；value：配置里的路径。
 */
function resolve_path(base, value) {
    if (!value)
        return value;
    return node_path_1.default.isAbsolute(value) ? value : node_path_1.default.resolve(base, value);
}
/**
 * 递归展开字符串里的 ${ENV}。
 * 参数 value：任意 JSON 值；env：变量表（默认 process.env）。
 */
function expand_env(value, env = process.env) {
    if (typeof value === 'string') {
        return value.replace(/\$\{([A-Z0-9_]+)\}/gi, (whole, name) => {
            const hit = env[name];
            if (hit === undefined) {
                process.stderr.write(`[config] 环境变量 ${name} 未设置，保留占位符\n`);
                return whole;
            }
            return hit;
        });
    }
    if (Array.isArray(value))
        return value.map((item) => expand_env(item, env));
    if (value && typeof value === 'object') {
        const out = {};
        for (const [key, item] of Object.entries(value)) {
            out[key] = expand_env(item, env);
        }
        return out;
    }
    return value;
}
/** 默认令牌（要被检测出来并告警）。 */
exports.DEFAULT_TOKEN = 'dev-token-change-me';
/**
 * 加载 Hub 配置。
 * 参数 file：配置路径；传 null 时只用默认值 + 环境变量。
 */
function load_hub_config(file = node_path_1.default.join(project_root(), 'config', 'hub.json')) {
    const base = project_root();
    const raw = file && node_fs_1.default.existsSync(file) ? expand_env(read_json_file(file)) : {};
    const cfg = {
        host: pick(raw.host, '0.0.0.0'),
        port: pick_number(raw.port, 9010),
        token: pick(raw.token, exports.DEFAULT_TOKEN),
        heartbeatSeconds: pick_number(raw.heartbeatSeconds, 10),
        heartbeatTimeoutSeconds: pick_number(raw.heartbeatTimeoutSeconds, 30),
        nodeHelloTimeoutSeconds: pick_number(raw.nodeHelloTimeoutSeconds, 10),
        ackTimeoutSeconds: pick_number(raw.ackTimeoutSeconds, 15),
        taskTimeoutSeconds: pick_number(raw.taskTimeoutSeconds, 600),
        maxAttempts: pick_number(raw.maxAttempts, 2),
        resultTtlSeconds: pick_number(raw.resultTtlSeconds, 900),
        maxResultBytes: pick_number(raw.maxResultBytes, 64 * 1024 * 1024),
        maxBufferedBytes: pick_number(raw.maxBufferedBytes, 512 * 1024 * 1024),
        maxUploadBytes: pick_number(raw.maxUploadBytes, 32 * 1024 * 1024),
        queuePumpMs: pick_number(raw.queuePumpMs, 250),
        sweepSeconds: pick_number(raw.sweepSeconds, 30),
        logFile: pick(raw.logFile, 'logs/hub.log'),
        logLevel: pick(raw.logLevel, 'info'),
        tlsCertFile: pick(raw.tlsCertFile, ''),
        tlsKeyFile: pick(raw.tlsKeyFile, ''),
    };
    cfg.logFile = resolve_path(base, cfg.logFile);
    if (cfg.tlsCertFile)
        cfg.tlsCertFile = resolve_path(base, cfg.tlsCertFile);
    if (cfg.tlsKeyFile)
        cfg.tlsKeyFile = resolve_path(base, cfg.tlsKeyFile);
    if (process.env.RELAY_TOKEN)
        cfg.token = process.env.RELAY_TOKEN;
    if (process.env.RELAY_PORT)
        cfg.port = Number(process.env.RELAY_PORT) || cfg.port;
    if (process.env.RELAY_HOST)
        cfg.host = process.env.RELAY_HOST;
    if (process.env.RELAY_TLS_CERT)
        cfg.tlsCertFile = node_path_1.default.resolve(process.env.RELAY_TLS_CERT);
    if (process.env.RELAY_TLS_KEY)
        cfg.tlsKeyFile = node_path_1.default.resolve(process.env.RELAY_TLS_KEY);
    return cfg;
}
/**
 * 加载节点配置。
 * 参数 file：配置路径；传 null 时只用默认值 + 环境变量。
 */
function load_node_config(file = node_path_1.default.join(project_root(), 'config', 'node.json')) {
    const base = project_root();
    const raw = file && node_fs_1.default.existsSync(file) ? expand_env(read_json_file(file)) : {};
    const capabilities = Array.isArray(raw.capabilities) && raw.capabilities.length
        ? raw.capabilities.map((item) => ({
            name: String(item.name),
            priority: Number.isFinite(item.priority) ? Number(item.priority) : 1,
            concurrency: Math.max(1, Number.isFinite(item.concurrency) ? Number(item.concurrency) : 1),
            meta: item.meta && typeof item.meta === 'object' ? item.meta : undefined,
        }))
        : [{ name: 'echo', priority: 1, concurrency: 4 }];
    const workers = {};
    for (const [name, item] of Object.entries((raw.workers || {}))) {
        workers[name] = {
            mode: String(item.mode || 'mock'),
            baseUrl: item.baseUrl ? String(item.baseUrl) : undefined,
            model: item.model ? String(item.model) : undefined,
            apiKeyEnv: item.apiKeyEnv ? String(item.apiKeyEnv) : undefined,
            voice: item.voice ? String(item.voice) : undefined,
            format: item.format ? String(item.format) : undefined,
            timeoutMs: Number.isFinite(item.timeoutMs) ? Number(item.timeoutMs) : 180000,
        };
    }
    // 节点可独立部署；只有显式指定注册表时才共享配置。
    const service_file = process.env.SYNLOVE_SERVICE_CONFIG || raw.serviceConfigFile;
    if (service_file) {
        const registry = read_json_file(resolve_path(base, String(service_file)));
        if (registry.version !== 1)
            throw new Error('serviceConfig.version 必须是 1');
        for (const [name, service] of Object.entries({ chat: 'llm', tts: 'tts', asr: 'asr' })) {
            const item = registry.services?.[service];
            if (!item || !workers[name])
                continue;
            if (typeof item.model !== 'string' || !item.model.trim() ||
                !Number.isFinite(item.timeoutSeconds) || item.timeoutSeconds <= 0 || item.timeoutSeconds > 3600) {
                throw new Error(`${service} 的 model 或 timeoutSeconds 无效`);
            }
            const prefix = `SYNLOVE_${service.toUpperCase()}`;
            const url = process.env[`${prefix}_BASE_URL`] || item.baseURL;
            const parsed = new URL(url);
            if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
                throw new Error(`${service}.baseURL 必须是无内嵌凭据的 HTTP(S) API 根地址`);
            }
            workers[name] = {
                ...workers[name], mode: 'openai', baseUrl: String(url).replace(/\/+$/, ''),
                model: item.model, apiKeyEnv: item.apiKeyEnv,
                timeoutMs: Number(item.timeoutSeconds) * 1000,
                ...(item.format ? { format: item.format } : {}),
                ...(item.voice ? { voice: item.voice } : {}),
            };
        }
    }
    const cfg = {
        hubUrl: pick(raw.hubUrl, 'ws://127.0.0.1:9010/ws/node'),
        token: pick(raw.token, exports.DEFAULT_TOKEN),
        nodeId: pick(raw.nodeId, ''),
        name: pick(raw.name, node_os_1.default.hostname()),
        heartbeatSeconds: pick_number(raw.heartbeatSeconds, 10),
        reconnectMinMs: pick_number(raw.reconnectMinMs, 1000),
        reconnectMaxMs: pick_number(raw.reconnectMaxMs, 15000),
        logFile: pick(raw.logFile, 'logs/node.log'),
        logLevel: pick(raw.logLevel, 'info'),
        capabilities,
        workers,
    };
    cfg.logFile = resolve_path(base, cfg.logFile);
    if (process.env.RELAY_TOKEN)
        cfg.token = process.env.RELAY_TOKEN;
    if (process.env.RELAY_HUB_URL)
        cfg.hubUrl = process.env.RELAY_HUB_URL;
    if (!cfg.nodeId) {
        cfg.nodeId = `${node_os_1.default.hostname().toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${Math.random().toString(36).slice(2, 6)}`;
    }
    return cfg;
}
/** 取字符串，空值用默认值。 */
function pick(value, fallback) {
    return typeof value === 'string' && value ? value : fallback;
}
/** 取数字，非法或缺失用默认值。 */
function pick_number(value, fallback) {
    return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}
//# sourceMappingURL=configLoader.js.map