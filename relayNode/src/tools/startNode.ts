/**
 * 工作节点启动器：读配置、按 workers 组装能力、连 Hub。
 *
 * 负责的功能：
 * - 解析命令行（`--config`、`--hub`、`--token`、`--node-id`、`--level`、`--drain`）；
 * - 读 config/node.json，用 `build_runner_from_config` 把 chat/tts/asr 装成能力；
 * - 起 nodeAgent 并打印本节点注册了哪些能力、连的是哪个 Hub；
 * - 退出信号时优雅关闭（先停止接新任务，再断开）。
 *
 * 注册参数（命令行）：
 *   --config   配置文件路径，默认 config/node.json
 *   --hub      覆盖 Hub WS 地址（也可用 RELAY_HUB_URL 环境变量）
 *   --token    覆盖令牌（长期部署请用 RELAY_TOKEN）
 *   --node-id  覆盖节点 id（同一台机器跑多个节点时用）
 *   --level    日志级别
 *   --drain    启动即排水（只连不接活，滚动升级用）
 */

import path from 'node:path';
import { load_node_config, project_root } from '../common/configLoader';
import { logger } from '../common/logger';
import { build_runner_from_config } from '../nodes/localClients';
import { nodeAgent } from '../nodes/nodeAgent';

async function main(argv: string[]): Promise<void> {
  const args = parse_args(argv);
  const config_path = args.config ? path.resolve(project_root(), String(args.config)) : undefined;
  const config = load_node_config(config_path ?? undefined);

  if (args.hub) config.hubUrl = String(args.hub);
  if (args.token) config.token = String(args.token);
  if (args['node-id']) config.nodeId = String(args['node-id']);
  if (args.level) config.logLevel = String(args.level) as any;

  const log = new logger(`node:${config.nodeId}`, {
    level: config.logLevel,
    file: config.logFile || null,
  });

  const { runner, skipped } = build_runner_from_config(config, log);
  if (!runner.names().length) {
    log.error('没有任何能力注册成功，检查 config/node.json 的 capabilities 与 workers');
    process.exit(1);
  }
  if (skipped.length) log.warn(`以下能力被跳过：${skipped.join('；')}`);

  const agent = new nodeAgent(config, runner, log);
  agent.start();
  log.info(`节点 ${config.name}（${config.nodeId}）启动，能力=${runner.names().join(',')}，Hub=${config.hubUrl}`);
  if (args.drain) {
    log.info('以排水模式启动：只连不接活');
  }

  let closing = false;
  const shutdown = (signal: string) => {
    if (closing) return;
    closing = true;
    log.info(`收到 ${signal}，关闭节点`);
    agent.stop();
    void log.close().then(() => process.exit(0));
    setTimeout(() => process.exit(0), 1500).unref();
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('uncaughtException', (error) => {
    log.error(`未捕获异常：${error.message}`);
  });
  process.on('unhandledRejection', (reason) => {
    log.error(`未处理的 Promise 拒绝：${String(reason)}`);
  });

  // 每 60 秒报一次存活，方便在长日志里定位
  setInterval(() => {
    log.info(`状态：${JSON.stringify(agent.status())}`);
  }, 60000).unref();
}

/** 解析 `--key value` 形式的参数。参数 argv：process.argv。 */
function parse_args(argv: string[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith('--')) continue;
    const key = token.slice(2);
    const next = argv[index + 1];
    if (next && !next.startsWith('--')) {
      out[key] = next;
      index += 1;
    } else {
      out[key] = true;
    }
  }
  return out;
}

void main(process.argv).catch((error) => {
  process.stderr.write(`节点启动失败：${(error as Error).message}\n`);
  process.exit(1);
});
