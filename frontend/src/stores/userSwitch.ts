/**
 * 换人信号（登录成功 / 登出 / 401 掉线）。
 *
 * 为什么单独一个叶子模块，而不是让 `authStore` 直接 import `chatStore`：
 * `api/client.ts` 已经 import 了 `authStore`，而 `chatStore` → `api/chat` → `api/client`
 * —— 把 chatStore 拉进 authStore 会形成静态循环依赖（ESM 能跑，但初始化顺序变脆）。
 * 这里用「注册-通知」把依赖方向反过来：双方都只依赖本模块，谁也不依赖谁。
 *
 * 为什么需要它：换人只清 localStorage 指针**不够**。store 里还留着上一个人的
 * `messages`，而 `enterChannel` 的「同渠道且已有消息」早退会让下一个人在同一 tab
 * 里直接看到上一场对话（自动恢复没有「点一下」这个人为闸门，换人必须当作硬边界）。
 */

type UserSwitchListener = () => void;

const listeners: UserSwitchListener[] = [];

/** 注册换人回调（会话状态拥有者调用，如 chatStore）。 */
export function onUserSwitch(listener: UserSwitchListener): void {
  listeners.push(listener);
}

/** 通知所有注册者：身份已变，上一个登录会话的状态作废。 */
export function notifyUserSwitch(): void {
  for (const listener of listeners) listener();
}
