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

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import type { log_level } from './logger';

/** Hub 配置。 */
export interface hub_config {
  /** 监听地址，默认 0.0.0.0（VPS 上要让 Caddy 从本机反代进来） */
  host: string;
  /** 监听端口，默认 9010 */
  port: number;
  /** 共享访问令牌，所有节点与客户端都带它；生产必须改掉默认值 */
  token: string;
  /** 心跳间隔秒，默认 10 */
  heartbeatSeconds: number;
  /** 多久没收到任何帧就判定掉线，默认 30（= 3 个心跳） */
  heartbeatTimeoutSeconds: number;
  /** 节点连上后多久内必须发 hello，默认 10 */
  nodeHelloTimeoutSeconds: number;
  /** 派单后等 ack 的时限，默认 15；超时视为该节点没接住，重新路由 */
  ackTimeoutSeconds: number;
  /** 单个任务的总时限，默认 600 */
  taskTimeoutSeconds: number;
  /** 同一任务最多派几次，默认 2（含首次） */
  maxAttempts: number;
  /** 结果（含二进制）保留秒数，默认 900；过期清理 */
  resultTtlSeconds: number;
  /** 单任务结果字节上限，默认 64MB */
  maxResultBytes: number;
  /** 所有任务结果加起来的内存上限，默认 512MB（超了先淘汰最老的已收齐结果） */
  maxBufferedBytes: number;
  /** 单次上传（ASR 音频）字节上限，默认 32MB */
  maxUploadBytes: number;
  /** 队列泵间隔毫秒，默认 250（有空闲节点就把排队任务派出去） */
  queuePumpMs: number;
  /** 清理与超时检查间隔秒，默认 30 */
  sweepSeconds: number;
  /** 日志文件，默认 logs/hub.log */
  logFile: string;
  /** 日志级别，默认 info */
  logLevel: log_level;
  /** TLS 证书文件（PEM）；与 tlsKeyFile 同时非空才启用 HTTPS/WSS，默认空=明文 */
  tlsCertFile: string;
  /** TLS 私钥文件（PEM），默认空 */
  tlsKeyFile: string;
}

/** 单个能力对应的本机服务。 */
export interface worker_config {
  /** 适配模式：gemma / breeze / whisper / openai / mock */
  mode: string;
  /** 上游服务根地址，例如 http://127.0.0.1:8080/v1 */
  baseUrl?: string;
  /** 模型名（chat/asr 用） */
  model?: string;
  /** 读哪个环境变量当 API key（为空表示不需要） */
  apiKeyEnv?: string;
  /** 音色（tts 用） */
  voice?: string;
  /** 音频格式（tts 用），默认 mp3 */
  format?: string;
  /** 上游超时毫秒，默认 180000 */
  timeoutMs?: number;
}

/** 工作节点配置。 */
export interface node_config {
  /** Hub 的 WS 地址，例如 ws://127.0.0.1:9010/ws/node */
  hubUrl: string;
  /** 与 Hub 共享的令牌 */
  token: string;
  /** 节点 id，空字符串表示用「主机名 + 随机后缀」自动生成 */
  nodeId: string;
  /** 人读的名字，显示在 /v1/nodes */
  name: string;
  /** 心跳间隔秒，默认 10 */
  heartbeatSeconds: number;
  /** 重连最小间隔毫秒，默认 1000 */
  reconnectMinMs: number;
  /** 重连最大间隔毫秒，默认 15000 */
  reconnectMaxMs: number;
  /** 日志文件，默认 logs/node.log */
  logFile: string;
  /** 日志级别，默认 info */
  logLevel: log_level;
  /** 本节点要注册的能力 */
  capabilities: Array<{ name: string; priority: number; concurrency: number; meta?: Record<string, unknown> }>;
  /** 能力名 → 本机服务配置 */
  workers: Record<string, worker_config>;
}

/**
 * 项目根目录。
 * 依据：本文件位于 <root>/src/common/ 或 <root>/dist/common/，往上两级即根。
 */
export function project_root(): string {
  return path.resolve(__dirname, '..', '..');
}

/**
 * 读 JSON 文件（容错 BOM；文件不存在或语法错误都抛带路径的明确错误）。
 * 参数 file：绝对路径。
 */
export function read_json_file(file: string): any {
  const raw = fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '');
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`配置文件不是合法 JSON：${file}（${(error as Error).message}）`);
  }
}

/**
 * 把配置里的相对路径按项目根解析（绝对路径原样返回）。
 * 参数 base：基准目录；value：配置里的路径。
 */
export function resolve_path(base: string, value: string): string {
  if (!value) return value;
  return path.isAbsolute(value) ? value : path.resolve(base, value);
}

/**
 * 递归展开字符串里的 ${ENV}。
 * 参数 value：任意 JSON 值；env：变量表（默认 process.env）。
 */
export function expand_env<T>(value: T, env: NodeJS.ProcessEnv = process.env): T {
  if (typeof value === 'string') {
    return value.replace(/\$\{([A-Z0-9_]+)\}/gi, (whole, name: string) => {
      const hit = env[name];
      if (hit === undefined) {
        process.stderr.write(`[config] 环境变量 ${name} 未设置，保留占位符\n`);
        return whole;
      }
      return hit;
    }) as unknown as T;
  }
  if (Array.isArray(value)) return value.map((item) => expand_env(item, env)) as unknown as T;
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out[key] = expand_env(item, env);
    }
    return out as unknown as T;
  }
  return value;
}

/** 默认令牌（要被检测出来并告警）。 */
export const DEFAULT_TOKEN = 'dev-token-change-me';

/**
 * 加载 Hub 配置。
 * 参数 file：配置路径；传 null 时只用默认值 + 环境变量。
 */
export function load_hub_config(file: string | null = path.join(project_root(), 'config', 'hub.json')): hub_config {
  const base = project_root();
  const raw = file && fs.existsSync(file) ? expand_env(read_json_file(file)) : {};
  const cfg: hub_config = {
    host: pick(raw.host, '0.0.0.0'),
    port: pick_number(raw.port, 9010),
    token: pick(raw.token, DEFAULT_TOKEN),
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
    logLevel: (pick(raw.logLevel, 'info') as log_level),
    tlsCertFile: pick(raw.tlsCertFile, ''),
    tlsKeyFile: pick(raw.tlsKeyFile, ''),
  };
  cfg.logFile = resolve_path(base, cfg.logFile);
  if (cfg.tlsCertFile) cfg.tlsCertFile = resolve_path(base, cfg.tlsCertFile);
  if (cfg.tlsKeyFile) cfg.tlsKeyFile = resolve_path(base, cfg.tlsKeyFile);
  if (process.env.RELAY_TOKEN) cfg.token = process.env.RELAY_TOKEN;
  if (process.env.RELAY_PORT) cfg.port = Number(process.env.RELAY_PORT) || cfg.port;
  if (process.env.RELAY_HOST) cfg.host = process.env.RELAY_HOST;
  if (process.env.RELAY_TLS_CERT) cfg.tlsCertFile = path.resolve(process.env.RELAY_TLS_CERT);
  if (process.env.RELAY_TLS_KEY) cfg.tlsKeyFile = path.resolve(process.env.RELAY_TLS_KEY);
  return cfg;
}

/**
 * 加载节点配置。
 * 参数 file：配置路径；传 null 时只用默认值 + 环境变量。
 */
export function load_node_config(file: string | null = path.join(project_root(), 'config', 'node.json')): node_config {
  const base = project_root();
  const raw = file && fs.existsSync(file) ? expand_env(read_json_file(file)) : {};
  const capabilities = Array.isArray(raw.capabilities) && raw.capabilities.length
    ? raw.capabilities.map((item: any) => ({
      name: String(item.name),
      priority: Number.isFinite(item.priority) ? Number(item.priority) : 1,
      concurrency: Math.max(1, Number.isFinite(item.concurrency) ? Number(item.concurrency) : 1),
      meta: item.meta && typeof item.meta === 'object' ? item.meta : undefined,
    }))
    : [{ name: 'echo', priority: 1, concurrency: 4 }];

  const workers: Record<string, worker_config> = {};
  for (const [name, item] of Object.entries((raw.workers || {}) as Record<string, any>)) {
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
    if (registry.version !== 1) throw new Error('serviceConfig.version 必须是 1');
    for (const [name, service] of Object.entries({ chat: 'llm', tts: 'tts', asr: 'asr' })) {
      const item = registry.services?.[service];
      if (!item || !workers[name]) continue;
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
  const cfg: node_config = {
    hubUrl: pick(raw.hubUrl, 'ws://127.0.0.1:9010/ws/node'),
    token: pick(raw.token, DEFAULT_TOKEN),
    nodeId: pick(raw.nodeId, ''),
    name: pick(raw.name, os.hostname()),
    heartbeatSeconds: pick_number(raw.heartbeatSeconds, 10),
    reconnectMinMs: pick_number(raw.reconnectMinMs, 1000),
    reconnectMaxMs: pick_number(raw.reconnectMaxMs, 15000),
    logFile: pick(raw.logFile, 'logs/node.log'),
    logLevel: (pick(raw.logLevel, 'info') as log_level),
    capabilities,
    workers,
  };
  cfg.logFile = resolve_path(base, cfg.logFile);
  if (process.env.RELAY_TOKEN) cfg.token = process.env.RELAY_TOKEN;
  if (process.env.RELAY_HUB_URL) cfg.hubUrl = process.env.RELAY_HUB_URL;
  if (!cfg.nodeId) {
    cfg.nodeId = `${os.hostname().toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${Math.random().toString(36).slice(2, 6)}`;
  }
  return cfg;
}

/** 取字符串，空值用默认值。 */
function pick(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback;
}

/** 取数字，非法或缺失用默认值。 */
function pick_number(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}
