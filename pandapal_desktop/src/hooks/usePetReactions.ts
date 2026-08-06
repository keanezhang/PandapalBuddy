/**
 * src/hooks/usePetReactions.ts
 *
 * 把「Agent 活动」映射到宠物动画：
 *   - 开始/进行中生成（streaming）      → running（常态）
 *   - 生成结束（streaming 由 true→false）→ waving（挥手一下）再回落 idle
 *   - 出错（failed）                     → 由 BackendProvider 在 ERROR 分支调用 petStore.pulse("failed")
 *
 * 说明：这里只消费 chatStore 的 streaming 派生态，不新增事件管线；失败态在 IPC 层就近触发。
 */

import { useEffect, useRef } from "react";
import { useIsStreaming } from "../store/chatStore";
import { usePetStore } from "../store/petStore";

export function usePetReactions(): void {
  const streaming = useIsStreaming();
  const prevStreaming = useRef(false);

  useEffect(() => {
    const { setBaseActivity, pulse } = usePetStore.getState();
    setBaseActivity(streaming);
    // 下降沿：一轮生成结束 → 挥手
    if (prevStreaming.current && !streaming) {
      pulse("waving", 1600);
    }
    prevStreaming.current = streaming;
  }, [streaming]);
}
