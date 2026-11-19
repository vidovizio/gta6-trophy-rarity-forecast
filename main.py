"""Offline week-one GTA VI trophy earn-rate forecaster."""

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
Z = (-1.645, -0.674, 0.0, 0.674, 1.645)
RELEASE = datetime(2026, 11, 19, tzinfo=timezone.utc)


def round4(value):
    scaled = value * 10000
    rounded = math.floor(scaled + 0.5)
    result = rounded / 10000
    return int(result) if result.is_integer() else result


def clamp_rate(rate):
    return min(99.99, max(0.01, rate))


def logit(rate):
    rate = clamp_rate(rate)
    return math.log(rate / (100 - rate))


def logistic(value):
    if value >= 0:
        return 100 / (1 + math.exp(-value))
    exp_value = math.exp(value)
    return 100 * exp_value / (1 + exp_value)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def linear_fit(xs, ys):
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    variance = sum((x - x_mean) ** 2 for x in xs)
    if variance <= 1e-12:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / variance
    intercept = y_mean - slope * x_mean
    if not math.isfinite(slope) or not math.isfinite(intercept):
        return None
    return slope, intercept, x_mean, math.sqrt(variance / len(xs))


def forecast_players(history, target_x, horizon_hours):
    points = []
    for read in history:
        n = read.get("players_tracked")
        if n is None or n <= 0:
            continue
        x = math.log1p(max(0.0, (parse_time(read["at"]) - RELEASE).total_seconds() / 3600))
        points.append((x, float(n), parse_time(read["at"])))
    if not points:
        return 0.0
    last_n = points[-1][1]
    latest = points[-1][2]
    cutoff = latest.timestamp() - 48 * 3600
    selected = [(x, math.log(n)) for x, n, at in points if at.timestamp() >= cutoff]
    if len(selected) < 2:
        selected = [(x, math.log(n)) for x, n, _ in points]
    fit = linear_fit([point[0] for point in selected], [point[1] for point in selected])
    if fit is None:
        return last_n
    slope, intercept, _, _ = fit
    proposed_growth = max(0.0, intercept + slope * target_x - math.log(last_n))
    day_ago = latest.timestamp() - 24 * 3600
    prior = next(((n, at) for _, n, at in reversed(points[:-1]) if at.timestamp() <= day_ago), None)
    if prior is not None and prior[0] > 0:
        elapsed_hours = (latest - prior[1]).total_seconds() / 3600
        recent_rate = max(0.0, math.log(last_n / prior[0]) / elapsed_hours) if elapsed_hours > 0 else 0.0
    elif len(points) > 1:
        prev_n, prev_at = points[-2][1], points[-2][2]
        elapsed_hours = (latest - prev_at).total_seconds() / 3600
        recent_rate = max(0.0, math.log(last_n / prev_n) / elapsed_hours) if elapsed_hours > 0 and prev_n > 0 else 0.0
    else:
        recent_rate = 0.0
    # Keep the forecasted tracked base from shrinking below its latest observation.
    return max(last_n, last_n * math.exp(min(proposed_growth, recent_rate * max(0.0, horizon_hours))))


def pava_nonincreasing(values):
    blocks = []
    for index, value in enumerate(values):
        blocks.append([index, index + 1, value, 1])
        while len(blocks) > 1 and blocks[-2][2] < blocks[-1][2]:
            right = blocks.pop()
            left = blocks.pop()
            weight = left[3] + right[3]
            mean = (left[2] * left[3] + right[2] * right[3]) / weight
            blocks.append([left[0], right[1], mean, weight])
    adjusted = [0.0] * len(values)
    for start, end, value, _ in blocks:
        adjusted[start:end] = [value] * (end - start)
    return adjusted


def trophy_chain(trophies):
    positions = {}
    for index, trophy in enumerate(trophies):
        description = trophy.get("description", "").strip()
        match = re.fullmatch(r"Complete Chapter ([1-4])\.", description, re.IGNORECASE)
        if match:
            positions.setdefault(int(match.group(1)), []).append(index)
        elif re.fullmatch(r"Complete the final mission\.", description, re.IGNORECASE):
            positions.setdefault(5, []).append(index)
        elif re.fullmatch(r"Attain 100% Completion\.", description, re.IGNORECASE):
            positions.setdefault(6, []).append(index)
        if trophy.get("tier", "").lower() == "platinum":
            positions.setdefault(7, []).append(index)
    order = []
    for key in range(1, 8):
        matches = positions.get(key, [])
        if len(matches) == 1:
            order.append(matches[0])
    return order


def forecast_case(case):
    history = case.get("history", [])
    trophies = case.get("trophies", [])
    horizon_minutes = max(0.0, float(case.get("horizon_minutes", 0)))
    horizon_days = horizon_minutes / 1440
    horizon_hours = horizon_minutes / 60
    target_time = parse_time(case["target"])
    target_x = math.log1p(max(0.0, (target_time - RELEASE).total_seconds() / 3600))
    rows = []
    medians = []
    sigmas = []

    if not history:
        for trophy in trophies:
            medians.append(1.0)
            sigmas.append(2.5)
    else:
        players_target = forecast_players(history, target_x, horizon_hours)
        for trophy_index, trophy in enumerate(trophies):
            observations = []
            for read in history:
                rates = read.get("rates", [])
                rate = rates[trophy_index] if trophy_index < len(rates) else None
                if rate is None:
                    continue
                n = read.get("players_tracked")
                if n is None or n <= 0:
                    continue
                at = parse_time(read["at"])
                x = math.log1p(max(0.0, (at - RELEASE).total_seconds() / 3600))
                rate = float(rate)
                observations.append((x, rate, float(n), at))

            if not observations:
                medians.append(1.0)
                sigmas.append(2.5)
                continue

            last_x, last_rate, last_n, last_at = observations[-1]
            cutoff = last_at.timestamp() - 48 * 3600
            recent = [item for item in observations if item[3].timestamp() >= cutoff]
            xs = [item[0] for item in recent]
            logits = [logit(item[1]) for item in recent]
            rate_fit = linear_fit(xs, logits) if len(set(xs)) >= 3 else None
            rate_prediction = None
            resid_std = 0.0
            leverage = 1.0
            if rate_fit is not None:
                slope, intercept, x_mean, x_spread = rate_fit
                rate_prediction = slope * target_x + intercept
                residuals = [y - (slope * x + intercept) for x, y in zip(xs, logits)]
                resid_std = math.sqrt(sum(residual * residual for residual in residuals) / max(1, len(residuals) - 2))
                if x_spread > 0:
                    leverage = 1 + max(0.0, target_x - x_mean) / x_spread

            count_points = [(math.log(item[2]), math.log(item[1] * item[2] / 100)) for item in observations if item[1] > 0 and item[2] > 0]
            count_fit = linear_fit([point[0] for point in count_points], [point[1] for point in count_points]) if len(count_points) >= 3 else None
            count_prediction = None
            if count_fit is not None and players_target > 0 and last_n > 0:
                beta, _, _, _ = count_fit
                # Limit noisy log-count fits before extrapolating far beyond the observations.
                beta = min(2.0, max(-1.0, beta))
                earned_last = max(0.0, last_rate * last_n / 100)
                if earned_last > 0:
                    predicted_earned = earned_last * (players_target / last_n) ** beta
                    count_rate = 100 * predicted_earned / players_target
                    if math.isfinite(count_rate):
                        count_prediction = logit(count_rate)

            predictions = [prediction for prediction in (rate_prediction, count_prediction) if prediction is not None and math.isfinite(prediction)]
            median_logit = sum(predictions) / len(predictions) if predictions else logit(last_rate)
            medians.append(clamp_rate(logistic(median_logit)))

            changes = [logit(b[1]) - logit(a[1]) for a, b in zip(observations, observations[1:])]
            change_mean = sum(changes) / len(changes) if changes else 0.0
            shock = math.sqrt(sum((change - change_mean) ** 2 for change in changes) / len(changes)) if changes else 0.0
            sigma = math.sqrt((0.5 * math.sqrt(horizon_days)) ** 2 + (resid_std * leverage) ** 2 + shock ** 2)
            sigmas.append(min(2.5, max(0.35, sigma)))

    chain = trophy_chain(trophies)
    if chain:
        adjusted = pava_nonincreasing([medians[index] for index in chain])
        for index, value in zip(chain, adjusted):
            medians[index] = clamp_rate(value)

    for trophy, median, sigma in zip(trophies, medians, sigmas):
        center = logit(median)
        quantiles = [round4(logistic(center + z * sigma)) for z in Z]
        quantiles = [min(100, max(0, value)) for value in quantiles]
        rows.append({"id": trophy["id"], "q": quantiles})
    return {"trophies": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        result = forecast_case(case)
        output = json.dumps(result, separators=(",", ":"), allow_nan=False) + "\n"
        if len(output.encode("utf-8")) > 65536:
            raise ValueError(f"{path.name} output exceeds 65536 bytes")
        (OUTPUT_DIR / f"{path.stem}.out").write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
