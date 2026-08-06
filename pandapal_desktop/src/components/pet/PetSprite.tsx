/**
 * src/components/pet/PetSprite.tsx
 *
 * 精灵图动画播放器（复刻自 kenshin-demo，组件化 + 从磁盘读取 webp）。
 *
 * - 通过 plugin-fs 读取 spritesheet.webp 字节 → Blob → ImageBitmap（避开 asset 协议配置）。
 * - 按 PET_ANIMATIONS 约定切帧；loop=false 的动作播完停在末帧（由上层 pulse 计时切回）。
 * - image-rendering: pixelated 保留像素风。
 */

import { useEffect, useRef } from "react";
import { readFile } from "@tauri-apps/plugin-fs";
import {
  FRAME_W,
  FRAME_H,
  PET_FPS,
  PET_ANIMATIONS,
  type PetAnimState,
} from "../../types/pet";

interface PetSpriteProps {
  /** spritesheet.webp 绝对路径 */
  spritesheetPath: string;
  /** 当前动作状态 */
  anim: PetAnimState;
  /** 显示尺寸（px），等比缩放单帧 */
  size?: number;
}

export function PetSprite({ spritesheetPath, anim, size = 96 }: PetSpriteProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const bitmapRef = useRef<ImageBitmap | null>(null);
  const animRef = useRef<PetAnimState>(anim);
  const rafRef = useRef<number | null>(null);

  // 让动画循环读到最新 anim，而不必重启 RAF
  animRef.current = anim;

  // 加载精灵图（路径变化时重载）
  useEffect(() => {
    let cancelled = false;
    let localBitmap: ImageBitmap | null = null;

    (async () => {
      try {
        const bytes = await readFile(spritesheetPath);
        if (cancelled) return;
        const blob = new Blob([bytes], { type: "image/webp" });
        localBitmap = await createImageBitmap(blob);
        if (cancelled) {
          localBitmap.close();
          return;
        }
        bitmapRef.current = localBitmap;
      } catch (err) {
        console.error("[pet] 加载精灵图失败:", spritesheetPath, err);
      }
    })();

    return () => {
      cancelled = true;
      if (localBitmap) localBitmap.close();
      bitmapRef.current = null;
    };
  }, [spritesheetPath]);

  // 动画循环
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let frame = 0;
    let acc = 0;
    let last = 0;
    let lastRow = -1;
    const frameDuration = 1000 / PET_FPS;

    const loop = (ts: number) => {
      rafRef.current = requestAnimationFrame(loop);
      const bitmap = bitmapRef.current;
      if (!bitmap) {
        last = ts;
        return;
      }
      const spec = PET_ANIMATIONS[animRef.current];

      // 切换动作时复位帧
      if (spec.row !== lastRow) {
        lastRow = spec.row;
        frame = 0;
        acc = 0;
      }

      if (!last) last = ts;
      acc += ts - last;
      last = ts;

      if (acc >= frameDuration) {
        acc -= frameDuration;
        if (spec.loop) {
          frame = (frame + 1) % spec.frames;
        } else if (frame < spec.frames - 1) {
          frame += 1; // 非循环：播到末帧停住
        }
      }

      const sx = frame * FRAME_W;
      const sy = spec.row * FRAME_H;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(bitmap, sx, sy, FRAME_W, FRAME_H, 0, 0, canvas.width, canvas.height);
    };

    rafRef.current = requestAnimationFrame(loop);
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, []);

  const h = Math.round((size * FRAME_H) / FRAME_W);

  return (
    <canvas
      ref={canvasRef}
      width={FRAME_W}
      height={FRAME_H}
      style={{
        width: size,
        height: h,
        imageRendering: "pixelated",
        pointerEvents: "none",
        userSelect: "none",
      }}
    />
  );
}
