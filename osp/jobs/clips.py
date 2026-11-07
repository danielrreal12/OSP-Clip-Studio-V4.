"""Export distinct highlight groups from the validated director timeline."""
import copy


def select_clips(plan, limit=3):
    segments = plan["segments"]
    total = sum(s["output_duration"] for s in segments)
    count = min(limit, max(1, int(total // 12)))
    target = min(30, total / count)
    groups, current, seconds = [], [], 0
    for segment in segments:
        if current and seconds + segment["output_duration"] > target:
            groups.append(current)
            current, seconds = [], 0
        current.append(segment)
        seconds += segment["output_duration"]
    if current:
        groups.append(current)
    if len(groups) > 1 and sum(s["output_duration"] for s in groups[-1]) < 12:
        groups[-2].extend(groups.pop())
    # If the validated timeline is one short story, export it intact.
    ranked = sorted(enumerate(groups), key=lambda pair: -sum(s["importance"] * s["output_duration"] for s in pair[1]) / sum(s["output_duration"] for s in pair[1]))
    clips = []
    for index, group in sorted(ranked[:limit]):
        clip = copy.deepcopy(plan)
        clip["segments"] = copy.deepcopy(group)
        clip["segments"][0]["transition_in"] = {"type": "cut", "duration": 0}
        clip["outro"] = None
        clip["meta"]["clip_score"] = round(sum(s["importance"] for s in group) / len(group), 3)
        clip["meta"]["source_group"] = index
        clips.append(clip)
    return clips
