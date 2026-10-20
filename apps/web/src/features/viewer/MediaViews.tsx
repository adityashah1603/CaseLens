import { Play, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import type { AudioLocator, VideoLocator } from "../../api/types.ts";
import { Button } from "../../components/ui/button.tsx";
import { formatMs } from "../../lib/format.ts";
import { scaleBox, type Rect } from "./geometry.ts";

function seek(el: HTMLMediaElement, ms: number): void {
  try {
    el.currentTime = ms / 1000;
  } catch {
    // Not seekable yet; `loadedmetadata` seeks again.
  }
}

/** Attaches the blob to the media element as an object URL, revoked when the blob changes. */
function useBlobSource(ref: RefObject<HTMLMediaElement | null>, blob: Blob): void {
  useEffect(() => {
    const el = ref.current;
    if (el === null) return;
    const url = URL.createObjectURL(blob);
    el.src = url;
    return () => {
      el.removeAttribute("src");
      URL.revokeObjectURL(url);
    };
  }, [ref, blob]);
}

/** Audio player that starts at the cited span and can replay exactly that span. */
export function AudioView({ blob, locator }: { blob: Blob; locator: AudioLocator }) {
  const ref = useRef<HTMLAudioElement>(null);
  const stopAtRef = useRef<number | null>(null);
  useBlobSource(ref, blob);

  const toStart = useCallback(() => {
    if (ref.current !== null) seek(ref.current, locator.start_ms);
  }, [locator.start_ms]);

  useEffect(() => {
    toStart();
  }, [toStart]);

  const playSpan = () => {
    const el = ref.current;
    if (el === null) return;
    seek(el, locator.start_ms);
    stopAtRef.current = locator.end_ms / 1000;
    void el.play().catch(() => undefined);
  };

  return (
    <div className="flex flex-col gap-2" data-testid="audio-view" data-start-ms={locator.start_ms} data-end-ms={locator.end_ms}>
      <dl className="grid grid-cols-2 gap-x-4 text-xs text-slate-600">
        <div>
          <dt className="text-slate-400">Cited span</dt>
          <dd className="tabular-nums">
            {formatMs(locator.start_ms)} to {formatMs(locator.end_ms)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-400">Speaker</dt>
          <dd data-testid="audio-speaker">{locator.speaker ?? "Unknown"}</dd>
        </div>
      </dl>
      <audio
        ref={ref}
        controls
        preload="auto"
        className="w-full"
        aria-label="Cited audio"
        onLoadedMetadata={toStart}
        onTimeUpdate={(e) => {
          const stopAt = stopAtRef.current;
          if (stopAt !== null && e.currentTarget.currentTime >= stopAt) {
            e.currentTarget.pause();
            stopAtRef.current = null;
          }
        }}
      />
      <div>
        <Button variant="secondary" size="sm" onClick={playSpan}>
          <Play aria-hidden /> Play cited span
        </Button>
      </div>
    </div>
  );
}

/** Video player paused on the cited frame with the detection bbox drawn over it. */
export function VideoView({ blob, locator }: { blob: Blob; locator: VideoLocator }) {
  const ref = useRef<HTMLVideoElement>(null);
  const [box, setBox] = useState<Rect | null>(null);
  const [atFrame, setAtFrame] = useState(true);
  useBlobSource(ref, blob);

  const measure = useCallback(() => {
    const el = ref.current;
    if (el === null || locator.bbox === null || locator.bbox === undefined) {
      setBox(null);
      return;
    }
    setBox(scaleBox(locator.bbox, { width: el.videoWidth, height: el.videoHeight }, { width: el.clientWidth, height: el.clientHeight }));
  }, [locator.bbox]);

  const toFrame = useCallback(() => {
    const el = ref.current;
    if (el === null) return;
    el.pause();
    seek(el, locator.start_ms);
    setAtFrame(true);
    measure();
  }, [locator.start_ms, measure]);

  useEffect(() => {
    const el = ref.current;
    if (el === null) return;
    el.pause();
    seek(el, locator.start_ms);
  }, [locator.start_ms]);

  useEffect(() => {
    const el = ref.current;
    if (el === null || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => {
      ro.disconnect();
    };
  }, [measure]);

  return (
    <div className="flex flex-col gap-2" data-testid="video-view" data-start-ms={locator.start_ms}>
      <dl className="grid grid-cols-3 gap-x-4 text-xs text-slate-600">
        <div>
          <dt className="text-slate-400">Cited time</dt>
          <dd className="tabular-nums">
            {formatMs(locator.start_ms)}
            {locator.end_ms !== null && locator.end_ms !== undefined && ` to ${formatMs(locator.end_ms)}`}
          </dd>
        </div>
        <div>
          <dt className="text-slate-400">Frame</dt>
          <dd className="tabular-nums" data-testid="video-frame">
            {locator.frame_idx ?? "n/a"}
          </dd>
        </div>
        <div>
          <dt className="text-slate-400">Track</dt>
          <dd>{locator.track_id ?? "n/a"}</dd>
        </div>
      </dl>
      <div className="relative w-full overflow-hidden rounded bg-black">
        <video
          ref={ref}
          controls
          muted
          preload="auto"
          className="block h-auto w-full"
          aria-label="Cited video"
          onLoadedMetadata={toFrame}
          onSeeked={measure}
          onTimeUpdate={(e) => {
            setAtFrame(Math.abs(e.currentTarget.currentTime * 1000 - locator.start_ms) < 500);
          }}
        />
        {box !== null && atFrame && (
          <div
            data-testid="video-bbox"
            aria-label="Cited detection"
            className="pointer-events-none absolute border-2 border-orange-400 shadow-[0_0_0_1px_rgba(0,0,0,0.6)]"
            style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
          />
        )}
      </div>
      <div>
        <Button variant="secondary" size="sm" onClick={toFrame}>
          <RotateCcw aria-hidden /> Back to cited frame
        </Button>
      </div>
    </div>
  );
}
