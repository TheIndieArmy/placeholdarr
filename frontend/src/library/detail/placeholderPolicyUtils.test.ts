/** @vitest-environment node */
import { describe, expect, it } from "vitest";
import {
  densityLocksEpisodePins,
  densityLocksSeasonPins,
  densityPolicyUnavailableReason,
} from "./placeholderPolicyUtils";

describe("density pin locks", () => {
  it("locks episode pins under season and series density", () => {
    expect(densityLocksEpisodePins("episode")).toBe(false);
    expect(densityLocksEpisodePins("season")).toBe(true);
    expect(densityLocksEpisodePins("series")).toBe(true);
  });

  it("locks season pins only under series density", () => {
    expect(densityLocksSeasonPins("episode")).toBe(false);
    expect(densityLocksSeasonPins("season")).toBe(false);
    expect(densityLocksSeasonPins("series")).toBe(true);
  });

  it("explains unavailable pins", () => {
    expect(densityPolicyUnavailableReason("episode", "season")).toMatch(/Not available in Season density/);
    expect(densityPolicyUnavailableReason("episode", "series")).toMatch(/Not available in Series density/);
    expect(densityPolicyUnavailableReason("season", "series")).toMatch(/Not available in Series density/);
    expect(densityPolicyUnavailableReason("episode", "episode")).toBeNull();
    expect(densityPolicyUnavailableReason("season", "season")).toBeNull();
  });
});
