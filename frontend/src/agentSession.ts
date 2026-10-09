import type { AgentStep } from '@/types';

/** 对话消息（含结构化的总控执行轨迹 kind='run'） */
export interface ChatMsg {
  role: string;
  content?: string;
  timestamp?: string;
  kind?: 'run';
  steps?: AgentStep[];
  status?: string;
}

export interface AgentSession {
  /** 本次前端会话内的消息（含 run 轨迹；后端历史里没有结构化轨迹） */
  messages: ChatMsg[];
  /** 进行中的总控 job：切换菜单/收起面板后据此恢复轮询 */
  runningJobId: string | null;
  /** job 开始时间（epoch ms），用于执行耗时显示 */
  runningStartedAt: number | null;
}

/**
 * 总控面板的会话级缓存（模块级单例，SPA 内切换菜单不丢）。
 *
 * 背景：ChatPanel 挂在工作台页内，切到其它侧边栏菜单再回来时组件整体卸载——
 * 此前 messages 由后端历史重建（结构化 run 轨迹丢失）、进行中的 job 轮询直接死亡。
 * 这里把消息与 running job 存在模块级 Map（key=projectKey），重挂载时原样恢复
 * 并继续跟踪同一个 job；离开期间已完成的步骤由 job.steps 一次性补齐。
 *
 * ⚠️ 仅内存缓存：整页刷新后 messages 回退到后端历史（run 轨迹不留），runningJobId
 *    清空——job 本身仍在后端继续跑，不影响生产，只是前端不再展示过程。
 */
const sessions = new Map<string, AgentSession>();

export function getAgentSession(projectKey: string): AgentSession {
  let s = sessions.get(projectKey);
  if (!s) {
    s = { messages: [], runningJobId: null, runningStartedAt: null };
    sessions.set(projectKey, s);
  }
  return s;
}

/**
 * 项目删除时清掉该键的会话缓存（ProjectsPage.handleDelete 删除成功后调用）。
 *
 * 为什么必须：缓存按 projectKey（= dir_key）作键，而「删除项目 → 同名重建」会得到
 * **同一个** dir_key —— 不清的话，重挂载的 ChatPanel 会命中旧缓存（loadHistory 对
 * 非空缓存直接短路、不回后端重拉），把已删项目的对话原样带回新项目面板；
 * 其中 run 执行轨迹（kind='run'）**只存在于这份缓存里**（后端历史不落轨迹），
 * 用户看到的「旧项目的操作播报」正是从这里来的。
 */
export function dropAgentSession(projectKey: string): void {
  sessions.delete(projectKey);
}
