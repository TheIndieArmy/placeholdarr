import type { ThemeMode } from "../../brandTypes";
import { alphaColor } from "./detailFormatters";

export function DetailHero(props: {
  title: string;
  year?: number | null;
  posterUrl?: string | null;
  backdropUrl?: string | null;
  posterFallback?: string;
  accent: { hex: string; icon: string };
  themeMode: ThemeMode;
}) {
  const isLight = props.themeMode === "light";
  const heroArtUrl = props.backdropUrl || props.posterUrl;
  return (
    <div
      className="relative h-[22rem] md:h-[30rem] lg:h-[34rem] overflow-hidden"
      style={
        heroArtUrl
          ? {
              backgroundImage: `linear-gradient(to right, ${isLight ? "rgba(238,243,248,0.90)" : "rgba(8,12,18,0.78)"} 18%, ${isLight ? "rgba(238,243,248,0.52)" : "rgba(8,12,18,0.45)"} 42%, ${isLight ? "rgba(238,243,248,0.10)" : "rgba(8,12,18,0.08)"}), url(${heroArtUrl})`,
              backgroundSize: "cover",
              backgroundPosition: "center 35%",
            }
          : { backgroundColor: alphaColor(props.accent.hex, isLight ? 0.14 : 0.2) }
      }
    >
      <div
        className="absolute inset-0"
        style={{
          background: isLight
            ? "linear-gradient(180deg, rgba(238,243,248,0) 32%, rgba(238,243,248,0.2) 58%, rgba(238,243,248,0.72) 80%, rgba(238,243,248,0.96) 93%, rgba(238,243,248,1) 100%)"
            : "linear-gradient(180deg, rgba(15,20,25,0) 32%, rgba(15,20,25,0.22) 58%, rgba(15,20,25,0.72) 80%, rgba(15,20,25,0.96) 93%, rgba(15,20,25,1) 100%)",
        }}
      />
      <div className="px-6 md:px-10 lg:px-12 relative h-full flex items-end pb-10 md:pb-14">
        <div className="flex gap-6 md:gap-10 items-end w-full">
          <div
            className={`flex-none w-40 h-60 md:w-52 md:h-[19.5rem] lg:w-56 lg:h-[21rem] rounded-2xl overflow-hidden border-2 shadow-[0_30px_80px_rgba(0,0,0,0.5)] ${isLight ? "border-[#d7e2f0] bg-white" : "border-[#424753]/40 bg-[#1e2430]"}`}
          >
            {props.posterUrl ? (
              <img src={props.posterUrl} alt="" className="w-full h-full object-cover" />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-slate-600 font-bold">
                {props.posterFallback ?? "—"}
              </div>
            )}
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-end pb-1 md:pb-2">
            {props.year ? (
              <div
                className="text-[22px] font-semibold tabular-nums md:text-[26px]"
                style={{ color: isLight ? props.accent.icon : props.accent.hex }}
              >
                {props.year}
              </div>
            ) : null}
            <h1
              className={`mt-1 text-4xl font-black font-headline tracking-tight leading-[1.02] md:text-5xl lg:text-6xl ${isLight ? "text-slate-900" : "text-white"}`}
            >
              {props.title}
            </h1>
          </div>
        </div>
      </div>
    </div>
  );
}
