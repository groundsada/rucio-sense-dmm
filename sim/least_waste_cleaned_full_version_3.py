import numpy as np
from typing import Iterable, Set, Tuple, List
from itertools import combinations
from math import inf
import matplotlib.pyplot as plt
import matplotlib.patches as patches

# Type definitions
Rect1D = Tuple[int, int, int]  # (start_time, end_time, bandwidth)
Rect2D = Tuple[int, int, int, int]  # (x1, x2, y1, y2)
Slots1D = Iterable[Rect1D]
Slots2D = Set[Rect2D]

# Tracking Bandwidth Usage
current_bandwidth_usage = {}  # {time_point: current_max_bandwidth_used}


def get_current_max_bandwidth(x1, x2):
    """Get the current maximum bandwidth used in the time range [x1, x2]"""
    return max((current_bandwidth_usage.get(t, 0) for t in range(x1, x2)), default=0)


def update_bandwidth_usage(x1, x2, y1, y2):
    """Update the bandwidth usage for the time range [x1, x2]"""
    for t in range(x1, x2): current_bandwidth_usage[t] = max(current_bandwidth_usage.get(t, 0), y2)


def get_next_slot(unavailable: Slots1D, total: Slots1D) -> List[Rect2D]:
    """
    Compute available regions by subtracting unavailable from total.
    """
    if not total: return []
    t_x1, t_x2, t_h = next(iter(total))

    # Sort unavailable slots
    result_regions = []
    current_y = 0
    for u_x1, u_x2, u_h in sorted(unavailable, key=lambda u: (u[0], u[2])):
        if u_h <= current_y: continue
        # Get the outer_slot
        if u_x1 > t_x1: result_regions.append((t_x1, u_x1, current_y, t_h))
        # Get the inner_slot
        if u_h > current_y: result_regions.append((t_x1, u_x1, current_y, u_h))
        current_y = u_h
    # Get the last slot
    if current_y < t_h: result_regions.append((t_x1, t_x2, current_y, t_h))

    # Filter and sort in one go
    return sorted(
        [(x1, x2, y1, y2) for x1, x2, y1, y2 in result_regions if x2 > x1 and y2 > y1],
        key=lambda r: (r[2], r[0], -r[3], r[1])
    )


def compute_slot_areas(slots: List[Rect2D]) -> List[int]:
    """Compute area of each slot for available list"""
    return [(x2 - x1) * (y2 - y1) for x1, x2, y1, y2 in slots]


def r_sorted_by_area(rules: List[Tuple[int, int]], reverse: bool = False) -> List[Tuple[int, int]]:
    """Generate [(size, r_id)] sorted by size"""
    return sorted([(size, idx + 1) for idx, (size, _) in enumerate(rules)], key=lambda x: x[0], reverse=reverse)


def ordinal(n: int) -> str:
    """Return ordinal string, e.g., 1->1st"""
    suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n if n < 20 else n % 10, 'th')
    return f"{n}{suffix}"


def can_fit_all_remaining_rule(r_remaining, current_slot_area):
    """Check if the outer slot can fit all remaining rules"""
    return sum(area for area, _ in r_remaining) <= current_slot_area


def allocate_rules_unified(
        slot_rect: Rect2D, rule: List[Tuple[int, int]], rule_r: List[Tuple[int, int]],
        unavailable_slots: Slots1D = None, strategy: str = 'auto'
) -> List:
    allocation = []
    x1, x2, y1, y2 = slot_rect
    slot_width = x2 - x1

    if not rule: return allocation

    current_max_used = get_current_max_bandwidth(int(x1), int(x2))
    effective_y1 = max(y1, current_max_used)
    available_height = y2 - effective_y1

    if available_height <= 0: return allocation

    def commit_alloc(rx1, rx2, ry1, ry2, rid):
        allocation.append((rx1, rx2, ry1, ry2, rid))
        update_bandwidth_usage(int(rx1), int(rx2), ry1, ry2)

    if strategy == 'auto':
        total_size = sum(area for area, _ in rule)
        can_fit_all = total_size <= (available_height * slot_width)
        if len(rule) == 1 or can_fit_all:
            strategy = 'fill'
        else:
            strategy = 'simple'

    if strategy == 'simple':
        y_cursor = effective_y1
        for area, rid in rule:
            h = area / slot_width
            commit_alloc(x1, x2, y_cursor, y_cursor + h, rid)
            y_cursor += h
        return allocation

    elif strategy == 'priority':
        # Calculate the sum of priority
        total_priority = sum(rule_r[rid - 1][1] for area, rid in rule)
        y_cursor = effective_y1

        for r_area, rid in rule:
            priority = rule_r[rid - 1][1]
            # One-liner for ratio calculation
            height_ratio = priority / total_priority if total_priority > 0 else 1.0 / len(rule)
            allocated_height = available_height * height_ratio

            # Calculate width logic kept exactly as is
            if allocated_height > 0:
                actual_width = r_area / allocated_height
            else:
                actual_width = slot_width
                allocated_height = r_area / actual_width if actual_width > 0 else 0

            if actual_width > slot_width:
                actual_width = slot_width
                allocated_height = r_area / actual_width

            x_end, y_end = x1 + actual_width, y_cursor + allocated_height
            commit_alloc(x1, x_end, y_cursor, y_end, rid)
            y_cursor = y_end

        return allocation

    elif strategy == 'fill':
        total_size = sum(area for area, _ in rule)

        # if there is only one left
        if len(rule) == 1:
            area, rid = rule[0]
            height = available_height
            width = area / height if height > 0 else 0

            if width > slot_width:
                width = slot_width
                height = area / width if width > 0 else 0

            commit_alloc(x1, x1 + width, effective_y1, effective_y1 + height, rid)
            return allocation

        # By priority logic
        if len(rule) > 1 and total_size <= (available_height * slot_width):
            all_priorities = [rule_r[rid - 1][1] for area, rid in rule]
            rule_data = []
            total_priority_sum = 0

            for area, rid in rule:
                original_priority = rule_r[rid - 1][1]
                priority_ratio = calculate_priority_bandwidth_ratio(original_priority, all_priorities)
                total_priority_sum += priority_ratio
                rule_data.append({'area': area, 'rid': rid, 'priority': original_priority, 'ratio': priority_ratio})

            has_conflict = False
            y_cursor_check = effective_y1

            for req_data in rule_data:
                height_ratio = req_data['ratio'] / total_priority_sum
                height = available_height * height_ratio
                width = req_data['area'] / height if height > 0 else 0

                # Check conflict
                x_end = x1 + width
                y_end = y_cursor_check + height
                if (unavailable_slots and has_bandwidth_conflict(x1, x_end, y_cursor_check, y_end, unavailable_slots) or
                        y_end > y2 or width > slot_width):
                    has_conflict = True
                    break

                req_data['height'] = height
                req_data['width'] = width
                y_cursor_check = y_end

            if has_conflict:
                return _allocate_with_conflict_handling(slot_rect, rule, rule_r, effective_y1, available_height,
                                                        slot_width)

            # Final allocation loop
            y_cursor = effective_y1
            for req_data in rule_data:
                width, height = req_data['width'], req_data['height']
                if width > slot_width:
                    width = slot_width
                    height = req_data['area'] / width if width > 0 else 0

                commit_alloc(x1, x1 + width, y_cursor, y_cursor + height, req_data['rid'])
                y_cursor += height

            return allocation
        else:
            return allocate_rules_unified(slot_rect, rule, rule_r, unavailable_slots, 'simple')

    return allocation


def _allocate_with_conflict_handling(
        slot_rect: Rect2D, rule: List[Tuple[int, int]], rule_r: List[Tuple[int, int]],
        effective_y1: float, available_height: float, slot_width: float
) -> List:
    allocation = []
    x1, x2, y1, y2 = slot_rect
    if not rule: return allocation

    initial_alloc = allocate_rules_unified(slot_rect, rule, rule_r, None, 'simple')
    max_y_used = max(alloc[3] for alloc in initial_alloc) if initial_alloc else effective_y1
    remaining_bw = y2 - max_y_used

    if remaining_bw <= 0: return initial_alloc

    p_sum = sum(rule_r[rid - 1][1] for _, rid in rule)
    priorities = [rule_r[rid - 1][1] for _, rid in rule] if p_sum > 0 else [1.0] * len(rule)
    p_sum = p_sum if p_sum > 0 else len(rule)

    y_cursor = effective_y1
    for i, (area, rid) in enumerate(rule):
        initial_h = area / slot_width
        add_h = remaining_bw * (priorities[i] / p_sum)
        h_up = initial_h + add_h
        w_up = area / h_up if h_up > 0 else slot_width

        if w_up > slot_width:
            w_up = slot_width
            h_up = area / w_up if w_up > 0 else 0

        allocation.append((x1, x1 + w_up, y_cursor, y_cursor + h_up, rid))
        update_bandwidth_usage(int(x1), int(x1 + w_up), y_cursor, y_cursor + h_up)
        y_cursor += h_up

    return allocation


def find_best_over_and_best_under(r_remaining, current_slot_area):
    """Find best fit group: one slightly under capacity, one slightly over."""
    best_under, best_under_sum = (), 0
    best_over, best_over_sum = (), inf
    for n in range(1, len(r_remaining) + 1):
        for combo in combinations(r_remaining, n):
            s = sum(area for area, _ in combo)
            if s <= current_slot_area and s > best_under_sum:
                best_under, best_under_sum = combo, s
                if s == current_slot_area: break
            elif s > current_slot_area and s < best_over_sum:
                best_over, best_over_sum = combo, s
        if best_under_sum == current_slot_area: break
    return best_under, best_under_sum, best_over, best_over_sum


def evaluate_blank2_area(best_over_sum, current_slot_area, current_slot_rect, next_slot_rect):
    """Determine waste when using 'Best over'"""
    x1, x2, _, _ = current_slot_rect
    overflow_h = (best_over_sum - current_slot_area) / (x2 - x1)
    return overflow_h * abs(next_slot_rect[1] - x2)


def apply_overflow_to_next_slots(slot_rects, slot_area_list, i, delta_h):
    """Adjust next slots based on overflow height"""
    for j in (i + 1, i + 2):
        if j >= len(slot_rects): break
        x1, x2, y1, y2 = slot_rects[j]
        new_y2 = y2 - delta_h
        if new_y2 <= y1:
            slot_rects.pop(j);
            slot_area_list.pop(j)
        else:
            slot_rects[j] = (x1, x2, y1, new_y2)
            slot_area_list[j] = (x2 - x1) * (new_y2 - y1)


def calculate_priority_bandwidth_ratio(original_prio, priority):
    unique = sorted(set(priority), reverse=True)
    return 1.0 if len(unique) == 1 else float(original_prio)


def has_bandwidth_conflict(x1, x2, y1, y2, unavailable_slots):
    for ux1, ux2, uh in unavailable_slots:
        if not (x2 <= ux1 or x1 >= ux2):
            if not (y1 >= uh or y2 <= 0): return True
    return False


def out_of_range(remaining_r, original_r_list, current_slot_area, total_available_area):
    if not remaining_r: return [], [], []
    eff_area = total_available_area if len(remaining_r) == len(
        original_r_list) and total_available_area else current_slot_area

    best_under, best_under_sum, _, _ = find_best_over_and_best_under(remaining_r, eff_area)
    if not best_under or best_under_sum > eff_area:
        return [], remaining_r[:]

    # Priority Check Logic
    best_group_sizes = [area for area, _ in best_under]
    candidates = {}
    for area, rid in remaining_r:
        if area in best_group_sizes: candidates.setdefault(area, []).append((area, rid))

    final_best = []
    used = set()
    for size in best_group_sizes:
        if size in candidates:
            best_cand = max(
                (c for c in candidates[size] if c not in used),
                key=lambda x: original_r_list[x[1] - 1][1], default=None
            )
            if best_cand: final_best.append(best_cand); used.add(best_cand)

    allocated_set = set(final_best)
    return final_best, [r for r in remaining_r if r not in allocated_set]


def init_bandwidth(unavailable_slots):
    global current_bandwidth_usage
    current_bandwidth_usage = {}
    for x1, x2, h in unavailable_slots:
        for t in range(x1, x2): current_bandwidth_usage[t] = h


def calc_available_area(slot_area_list):
    even_sum = sum(slot_area_list[i] for i in range(1, len(slot_area_list) - 1, 2)) if len(slot_area_list) > 1 else 0
    return even_sum + (slot_area_list[-1] if slot_area_list else 0)


def best_fit_allocation(r_remaining, current_slot_area, current_slot_rect, slot_rects, slot_area_list, rule_r, i,
                        slot_index):
    result, allocation = [], []
    best_under, best_under_sum, best_over, best_over_sum = find_best_over_and_best_under(r_remaining, current_slot_area)

    blank_2 = evaluate_blank2_area(best_over_sum, current_slot_area, current_slot_rect,
                                   slot_rects[i + 1]) if best_over and i + 1 < len(slot_rects) else inf

    best_fit_group, use_overflow = None, False
    if best_under and best_under_sum <= current_slot_area:
        best_fit_group = best_under
        alloc = allocate_rules_unified(current_slot_rect, best_fit_group, rule_r, None, 'priority')
        allocation.extend(alloc)
    elif best_over and blank_2 < current_slot_area:
        best_fit_group, use_overflow = best_over, True
        alloc = allocate_rules_unified(current_slot_rect, best_fit_group, rule_r, None, 'simple')
        allocation.extend(alloc)
    else:
        result.append(f"No rule fit in the {ordinal(slot_index)} area")
        return False, result, allocation, r_remaining

    result.append(f"rule {list(best_fit_group)} fitted in the {ordinal(slot_index)} area")
    for val in best_fit_group: r_remaining.remove(val)

    if use_overflow and i + 1 < len(slot_rects):
        delta_h = (best_over_sum - current_slot_area) / (current_slot_rect[1] - current_slot_rect[0])
        apply_overflow_to_next_slots(slot_rects, slot_area_list, i, delta_h)

    return True, result, allocation, r_remaining


def minimum_rule_check(r_remaining, current_slot_area, current_slot_rect, slot_rects, i, slot_index):
    result, allocation = [], []
    min_rule_size = min(area for area, _ in r_remaining)
    if min_rule_size <= current_slot_area: return False, result, allocation, r_remaining

    next_slot_width = slot_rects[i + 1][1] - current_slot_rect[1]
    max_height_extend = current_slot_area / next_slot_width
    current_w = current_slot_rect[1] - current_slot_rect[0]
    current_h = current_slot_rect[3] - current_slot_rect[2]

    if ((min_rule_size / current_w) - current_h) > max_height_extend:
        result.append(f"No rule fit in the {ordinal(slot_index)} area")
        return True, result, allocation, r_remaining

    min_rule = min(r_remaining, key=lambda x: x[0])
    allocation.extend(allocate_rules_unified(current_slot_rect, [min_rule], None, None, 'simple'))
    r_remaining.remove(min_rule)
    return True, result, allocation, r_remaining


def check_slot_is_empty(slot_index, slot_rects, allocation):
    if slot_index >= len(slot_rects): return False
    sx1, sx2, sy1, sy2 = slot_rects[slot_index]
    for x1, x2, y1, y2, rid in allocation:
        if not (x2 <= sx1 or x1 >= sx2 or y2 <= sy1 or y1 >= sy2): return False
    return True


def merge_two_slots(slot1, slot2):
    """Merge two rectangles into an L-shaped polygon for vertices"""
    x1_r1, x2_r1, y1_r1, y2_r1 = slot1
    x1_r2, x2_r2, y1_r2, y2_r2 = slot2
    vertices = [(0, 0), (x2_r1, 0), (x2_r1, y2_r1), (x2_r2, y1_r2), (x2_r2, y2_r2), (0, y2_r2)]
    area = (x2_r1 - x1_r1) * (y2_r1 - y1_r1) + (x2_r2 - x1_r2) * (y2_r2 - y1_r2)
    return vertices, area


def merge_new_slot(new_merged_slot, slot):
    """Merge previous slot with new slot"""
    vertices = new_merged_slot['vertices']
    x1_r, x2_r, y1_r, y2_r = slot
    max_x, max_y = max(v[0] for v in vertices), max(v[1] for v in vertices)
    new_vertices = [v for v in vertices if not ((v[0] == max_x and v[1] == max_y) or (v[0] == 0 and v[1] == max_y))]
    new_vertices.extend([(max_x, y1_r), (x2_r, y1_r), (x2_r, y2_r), (0, y2_r)])
    new_area = new_merged_slot['area'] + (x2_r - x1_r) * (y2_r - y1_r)
    return new_vertices, new_area


def allocate_rule_in_merged_slot(
        merged_info: dict,
        rule: List[Tuple[int, int]],
        rule_r: List[Tuple[int, int]],
        unavailable_slots: Slots1D
) -> List:
    """
    Allocate rules in merged slot area
    Args:
        merged_info: Dictionary containing merged slot information
        rule: List of rules to allocate [(area, rid), ...]
        rule_r: Original rule list [(size, priority), ...]
        unavailable_slots: Unavailable slots

    Returns:
        allocation: List of allocations
    """
    allocation = []

    if not rule: return allocation

    component_rects = merged_info.get('component_rects', [])
    if not component_rects: return allocation

    borrowable_rects = merged_info.get('borrowable_rects', [])
    all_rects = component_rects + borrowable_rects

    # Find the time and bandwidth boundaries
    max_time = max(rect[1] for rect in all_rects)
    min_bandwidth = min(rect[2] for rect in all_rects)
    max_bandwidth = max(rect[3] for rect in all_rects)

    # Start from the minimum available bandwidth (usually 0)
    start_bandwidth = min_bandwidth

    # Allocate rules one by one, stacked vertically
    y_cursor = start_bandwidth

    for req_area, rid in rule:
        # Calculate remaining bandwidth
        remaining_bandwidth = max_bandwidth - y_cursor
        if remaining_bandwidth <= 0: continue

        # Strategy: Use as much width as possible to minimize height
        # Find the maximum continuous time width available
        max_width = 0
        best_time_range = None
        # Try different starting times
        for rect in all_rects:
            rx1, rx2, ry1, ry2 = rect
            # Check if this rect has bandwidth in our range
            if ry2 <= y_cursor:
                continue  # This rect is below our cursor
            # Check for unavailable conflicts
            has_conflict = False
            for ux1, ux2, uh in unavailable_slots:
                if not (rx2 <= ux1 or rx1 >= ux2):  # Time overlap
                    if uh > y_cursor:  # Blocks our bandwidth
                        has_conflict = True
                        break
            if not has_conflict:
                width = rx2 - rx1
                if width > max_width:
                    max_width = width
                    best_time_range = (rx1, rx2)

        if best_time_range is None:
            # Fallback: use the first available rect
            first_rect = all_rects[0]
            best_time_range = (first_rect[0], first_rect[1])
            max_width = first_rect[1] - first_rect[0]

        x_start, x_end = best_time_range

        # Calculate height needed
        height_needed = req_area / max_width

        # Check if height fits in remaining bandwidth
        if height_needed > remaining_bandwidth:
            # Use all remaining bandwidth and extend time
            actual_height = remaining_bandwidth
            actual_width = req_area / actual_height
            actual_x_end = x_start + actual_width
            # Cap at max time
            if actual_x_end > max_time:
                actual_x_end = max_time
                actual_width = actual_x_end - x_start
                actual_height = req_area / actual_width
        else:
            # Fits within remaining bandwidth
            actual_height = height_needed
            actual_x_end = x_end

        y_start = y_cursor
        y_end = y_cursor + actual_height
        # Ensure we don't exceed boundaries
        y_end = min(y_end, max_bandwidth)
        actual_x_end = min(actual_x_end, max_time)

        allocation.append((x_start, actual_x_end, y_start, y_end, rid))
        update_bandwidth_usage(int(x_start), int(actual_x_end), y_start, y_end)

        # Move cursor up
        y_cursor = y_end

    return allocation


def progressive_inner_outer_merge(
        start_outer_index: int,
        slot_rects: List[Rect2D],
        slot_area_list: List[int],
        allocation: List,
        r_remaining: List[Tuple[int, int]],
        rule_r: List[Tuple[int, int]],
        unavailable_slots: Slots1D,
        total_time: int = None,
        total_bandwidth: float = None
) -> Tuple[bool, dict, int, List]:
    """
    Progressive merging with alternating inner-outer pattern:
    1. If previous inner (e.g., slot2) is empty
    2. Merge: outer3 + inner2 → updated_outer3
    3. Check if updated_outer3 can fit all (including borrowable areas)
    4. If not: Merge: inner4 + inner2 → updated_inner4
    5. Then: Merge: outer5 + updated_inner4 → updated_outer5
    6. Check if updated_outer5 can fit all (including borrowable areas)
    7. If not: Merge: inner6 + updated_inner4 → updated_inner6
    """
    outer_index = start_outer_index

    # Check if previous inner slot exists and is empty
    if outer_index < 1: return False, {}, 0, []

    prev_inner_index = outer_index - 1
    if prev_inner_index % 2 == 0:  # Should be odd (inner slot)
        return False, {}, 0, []

    if not check_slot_is_empty(prev_inner_index, slot_rects, allocation):
        return False, {}, 0, []

    # Track updated slots
    prev_inner_slot = slot_rects[prev_inner_index]
    current_outer_slot = slot_rects[outer_index]

    # Step 1: Merge outer with previous inner
    # outer3 + inner2 → updated_outer3
    vertices, area = merge_two_slots(prev_inner_slot, current_outer_slot)
    merged_area = slot_area_list[prev_inner_index] + slot_area_list[outer_index]
    updated_outer = {
        'vertices': vertices,
        'area': merged_area,
        'bounding_box': (0, max(v[0] for v in vertices), 0, max(v[1] for v in vertices)),
        'component_rects': [prev_inner_slot, current_outer_slot]
    }

    # Extend with future borrowable areas
    if total_time is not None and total_bandwidth is not None:
        updated_outer = extend_merged_area_with_future(
            updated_outer, total_time, total_bandwidth,
            unavailable_slots, current_bandwidth_usage
        )

        check_area = updated_outer.get('extended_area', merged_area)
    else:
        check_area = merged_area

    # Check if updated_outer can fit all
    if can_fit_all_remaining_rule(r_remaining, check_area):
        # Use irregular polygon allocation
        alloc = allocate_rule_in_merged_slot(updated_outer, r_remaining, rule_r, unavailable_slots)

        slots_consumed = len(updated_outer['component_rects'])
        return True, updated_outer, slots_consumed, alloc

    # Track the updated inner (starts with prev_inner)
    updated_inner = {
        'vertices': [(0, 0), (prev_inner_slot[1], 0), (prev_inner_slot[1], prev_inner_slot[3]),
                     (0, prev_inner_slot[3])],
        'area': slot_area_list[prev_inner_index],
        'bounding_box': prev_inner_slot,
        'component_rects': [prev_inner_slot]
    }

    # Continue alternating: inner merges with inner, outer merges with inner
    next_inner_index = outer_index + 1
    next_outer_index = outer_index + 2
    step = 2

    while next_outer_index < len(slot_rects):
        # Step: Merge next_inner with updated_inner → new updated_inner
        # inner4 + inner2 → updated_inner4
        if next_inner_index >= len(slot_rects):
            break

        next_inner_slot = slot_rects[next_inner_index]
        vertices, area = merge_new_slot(updated_inner, next_inner_slot)

        updated_inner = {
            'vertices': vertices,
            'area': updated_inner['area'] + slot_area_list[next_inner_index],
            'bounding_box': (0, max(v[0] for v in vertices), 0, max(v[1] for v in vertices)),
            'component_rects': updated_inner['component_rects'] + [next_inner_slot]
        }

        step += 1

        # Step: Merge next_outer with updated_inner → new updated_outer
        # outer5 + updated_inner4 → updated_outer5
        next_outer_slot = slot_rects[next_outer_index]
        vertices, area = merge_new_slot(updated_inner, next_outer_slot)

        updated_outer = {
            'vertices': vertices,
            'area': updated_inner['area'] + slot_area_list[next_outer_index],
            'bounding_box': (0, max(v[0] for v in vertices), 0, max(v[1] for v in vertices)),
            'component_rects': updated_inner['component_rects'] + [next_outer_slot]
        }
        step += 1

        # Extend with borrowable areas
        if total_time is not None and total_bandwidth is not None:
            updated_outer = extend_merged_area_with_future(
                updated_outer, total_time, total_bandwidth,
                unavailable_slots, current_bandwidth_usage
            )
            check_area = updated_outer.get('extended_area', updated_outer['area'])
            # if 'extended_area' in updated_outer:
        else:
            check_area = updated_outer['area']

        # Check if updated_outer can fit all
        if can_fit_all_remaining_rule(r_remaining, check_area):
            # Use irregular polygon allocation
            alloc = allocate_rule_in_merged_slot(updated_outer, r_remaining, rule_r, unavailable_slots)

            slots_consumed = len(updated_outer['component_rects'])
            return True, updated_outer, slots_consumed, alloc

        # Move to next pair
        next_inner_index = next_outer_index + 1
        next_outer_index = next_outer_index + 2

    # Even if failed, return the last merged state
    if 'updated_outer' in locals() and updated_outer:
        slots_consumed = len(updated_outer.get('component_rects', []))
        return False, updated_outer, slots_consumed, []
    else:
        return False, {}, 0, []


def find_borrowable_areas(
        current_time_end: int,
        total_time: int,
        total_bandwidth: float,
        unavailable_slots: Slots1D,
) -> List[Rect2D]:
    """
    For the outer slot, if the area shows that it can fit all the rules
    it can borrow the slot area after it (due to it may cause some waste)
    """
    if current_time_end >= total_time: return []
    # temp total area
    future_total = [(current_time_end, total_time, total_bandwidth)]

    # get the unavailable slot after current slot
    # set the starter time as the end of current slot
    adjusted_unavailable = [
        (max(x1, current_time_end), x2, h)
        for x1, x2, h in unavailable_slots
        if x2 > current_time_end and x2 > max(x1, current_time_end)
    ]

    # Calculate the area which can be added to current area
    borrowable_rects = get_next_slot(adjusted_unavailable, future_total)

    return borrowable_rects


def extend_merged_area_with_future(
        merged_info: dict,
        total_time: int,
        total_bandwidth: float,
        unavailable_slots: Slots1D,
        current_bandwidth_usage: dict
) -> dict:
    """
    If needed: add the area which can be borrowed to the current slot
    """

    component_rects = merged_info.get('component_rects', [])
    if not component_rects: return merged_info
    current_max_time = max(rect[1] for rect in component_rects)

    # get the area can be added to current slot
    borrowable_areas = find_borrowable_areas(
        current_max_time, total_time, total_bandwidth,
        unavailable_slots, current_bandwidth_usage
    )

    if not borrowable_areas:
        return merged_info

    # calculate the total area of added area
    borrowable_area = sum((x2 - x1) * (y2 - y1) for x1, x2, y1, y2 in borrowable_areas)

    # update the slot information
    extended_info = merged_info.copy()
    extended_info['borrowable_rects'] = borrowable_areas
    extended_info['extended_area'] = merged_info['area'] + borrowable_area
    extended_info['all_rects'] = component_rects + borrowable_areas

    return extended_info


def last_slot(r_remaining, current_slot_area, current_slot_rect, rule_r, unavailable_slots, total_available_area,
              slot_index, slot_rects, slot_area_list, allocation, current_slot_index):
    result, allocation_result = [], []
    effective_slot_area = current_slot_area
    if current_slot_index > 0:
        prev_idx = current_slot_index - 1
        if prev_idx % 2 == 1 and check_slot_is_empty(prev_idx, slot_rects, allocation):
            effective_slot_area += slot_area_list[prev_idx]
            # Check recursive previous empty slots
            chk = prev_idx - 2
            while chk >= 0 and chk % 2 == 1 and check_slot_is_empty(chk, slot_rects, allocation):
                effective_slot_area += slot_area_list[chk];
                chk -= 2

    if can_fit_all_remaining_rule(r_remaining, effective_slot_area):
        result.append(f"All remaining rule {r_remaining} fitted in the {ordinal(slot_index)} area")
        if effective_slot_area > current_slot_area:

            success, merge_info, slots_consumed, alloc = progressive_inner_outer_merge(
                current_slot_index, slot_rects, slot_area_list, allocation, r_remaining, rule_r, unavailable_slots
            )
            if success:
                allocation_result.extend(alloc)
                return result, allocation_result
        else:
            allocation_result.extend(
                allocate_rules_unified(current_slot_rect, r_remaining, rule_r, unavailable_slots, 'fill'))
        return result, allocation_result

    best_group, _ = out_of_range(r_remaining, rule_r, effective_slot_area, total_available_area)
    if best_group:
        allocation_result.extend(allocate_rules_unified(current_slot_rect, best_group, rule_r, None, 'priority'))
    return result, allocation_result


def find_r_slot_with_allocation(
        r_list: List[Tuple[int, int]],
        slot_area_list: List[int],
        slot_rects: List[Rect2D],
        unavailable_slots: Slots1D,
        rule_r: List[Tuple[int, int]],
        total_time: int = None,
        total_bandwidth: float = None,
        enable_advanced_features: bool = True
) -> Tuple[List[str], List[Tuple[int, int, float, float, int]], float, float]:
    """
    Main allocation function
    """
    # Initialize bandwidth tracking
    init_bandwidth(unavailable_slots)

    r_remaining = r_list[:]
    # Calculate the total size of the rule needed
    total_r_area = sum(area for area, _ in r_remaining)
    # Calculate the total area that we can put rules in: inner slot + last slot
    # All the even_slot are the inner slot
    total_available_area = calc_available_area(slot_area_list)

    result = []
    allocation = []
    slot_index = 1
    i = 0

    # We do compare between: sum of the remaining rules and current outer slot:
    # If everything fitted, then fit in the outer slot
    compare_mode = True

    while r_remaining and i < len(slot_area_list):
        current_slot_area = slot_area_list[i]
        current_slot_rect = slot_rects[i]

        # Handle last slot specially: We need to deal with the out_of_range situation
        if i == len(slot_area_list) - 1:
            res, alloc = last_slot(
                r_remaining, current_slot_area, current_slot_rect,
                rule_r, unavailable_slots, total_available_area, slot_index,
                slot_rects, slot_area_list, allocation, i
            )
            result.extend(res)
            allocation.extend(alloc)
            return result, allocation, total_available_area, total_r_area

        # Compare mode: check if all remaining fit in current slot
        if compare_mode:
            # Check if the previous inner slot is empty, if does, then merge the previous inner slot and the outer slot
            if enable_advanced_features:
                success, merge_info, slots_consumed, alloc = progressive_inner_outer_merge(
                    i, slot_rects, slot_area_list, allocation, r_remaining, rule_r, unavailable_slots,
                    total_time, total_bandwidth
                )
                if success:
                    # inner slot is empty. successfully merged, and can be fitted
                    result.append(
                        f"All remaining rules fitted using progressive merge (consumed {slots_consumed} slots)")
                    allocation.extend(alloc)
                    r_remaining = []
                    return result, allocation, total_available_area, total_r_area

            # inner slot is not empty
            if can_fit_all_remaining_rule(r_remaining, current_slot_area):
                result.append(f"All remaining rule {r_remaining} fitted in the {ordinal(slot_index)} area")
                alloc = allocate_rules_unified(current_slot_rect, r_remaining, rule_r, unavailable_slots, 'fill')
                allocation.extend(alloc)
                return result, allocation, total_available_area, total_r_area

            # Move to next slot
            i += 1
            slot_index += 1
            compare_mode = False
            continue

        # Check if minimum rule size exceeds current slot
        if enable_advanced_features:
            should_continue, res, alloc, r_remaining = minimum_rule_check(
                r_remaining, current_slot_area, current_slot_rect,
                slot_rects, i, slot_index
            )

            if should_continue:
                result.extend(res)
                allocation.extend(alloc)
                i += 1
                slot_index += 1
                compare_mode = True
                continue

        # Find and allocate best fit group
        if enable_advanced_features:
            allocated, res, alloc, r_remaining = best_fit_allocation(
                r_remaining, current_slot_area, current_slot_rect,
                slot_rects, slot_area_list, rule_r, i, slot_index
            )
            result.extend(res)
            allocation.extend(alloc)
        else:
            best_under, best_under_sum, _, _ = find_best_over_and_best_under(r_remaining, current_slot_area)

            if best_under and best_under_sum <= current_slot_area:
                result.append(f"Rules {list(best_under)} fitted in the {ordinal(slot_index)} area")
                alloc = allocate_rules_unified(current_slot_rect, best_under, rule_r, None, 'priority')
                allocation.extend(alloc)
                for val in best_under:
                    r_remaining.remove(val)
            else:
                result.append(f"No rule fit in the {ordinal(slot_index)} area")

        i += 1
        slot_index += 1
        compare_mode = True

    return result, allocation, total_available_area, total_r_area


def check_allocation_validity(allocations: List[Tuple[float, float, float, float, int]],
                              main_slot: Tuple[float, float],
                              request_r: List[Tuple[int, int]]) -> Tuple[bool, List[str]]:
    """
    check if the allocation extended of the main slot

    Returns:
        (is_valid, violation_messages)
        - is_valid: True we can use previous allocation result
    """
    bandwidth_limit, time_limit = main_slot
    is_valid = True
    if not allocations: return True, []

    # check every allocation
    for x1, x2, y1, y2, rid in allocations:
        size, priority = request_r[rid - 1]

        # time limit
        if x2 > time_limit:
            is_valid = False

        # bandwidth limit
        if y2 > bandwidth_limit:
            is_valid = False

        # check the actual area = the input area
        actual_area = (x2 - x1) * (y2 - y1)
        # check if the float effect the "unequal"
        if abs(actual_area - size) > 0.1:
            is_valid = False

    return is_valid, []


def run_find_least_waste(unavailable_slots, main_slot, request_r):
    bandwidth, time = main_slot
    total_slots = [(0, time, bandwidth)]
    slot_rects = get_next_slot(unavailable_slots, total_slots)
    slot_areas = compute_slot_areas(slot_rects)
    request_areas = r_sorted_by_area(request_r)

    result_texts, allocations, total_available_area, total_r_area = find_r_slot_with_allocation(
        request_areas, slot_areas, slot_rects, unavailable_slots, request_r, time, bandwidth
    )

    visualize_integrated_schedule(request_r, unavailable_slots, total_slots, allocations)


def run_find_least_waste_v2(unavailable_slots, main_slot, request_r, max_iterations=10):
    """
    firstly, check the is_valid, if is_valid = false, remove the smallest request, allocated again
    """
    bandwidth, time = main_slot
    total_slots = [(0, time, bandwidth)]
    remaining_indices = list(range(len(request_r)))
    dropped_indices = []
    iteration = 0

    while iteration < max_iterations:
        iteration += 1

        # the rule set is empty
        if not remaining_indices:
            return [], list(range(len(request_r))), iteration

        # allocated the rules
        slot_rects = get_next_slot(unavailable_slots, total_slots)
        slot_areas = compute_slot_areas(slot_rects)

        request_areas = []
        for orig_idx in remaining_indices:
            size, priority = request_r[orig_idx]
            request_areas.append((size, orig_idx + 1))

        request_areas_sorted = sorted(request_areas, key=lambda x: x[0])

        result_texts, allocations, total_available_area, total_r_area = \
            find_r_slot_with_allocation(
                request_areas_sorted, slot_areas, slot_rects, unavailable_slots, request_r,
                enable_advanced_features=False
            )

        is_valid, _ = check_allocation_validity(allocations, main_slot, request_r)

        if is_valid:
            visualize_integrated_schedule(request_r, unavailable_slots, total_slots, allocations)
            return allocations, dropped_indices, iteration

        else:
            # find the minimum rule size
            min_size = float('inf')
            min_idx = None
            for idx in remaining_indices:
                size, priority = request_r[idx]
                if size < min_size:
                    min_size = size
                    min_idx = idx

            if min_idx is None:
                break

            # delete the minimum
            remaining_indices.remove(min_idx)
            dropped_indices.append(min_idx)

    return allocations, dropped_indices, iteration


def final_allocation(unavailable_slots, main_slot, request_r):
    """
    Try run_find_least_waste first, if there is conflict, then do v2
    """
    bandwidth, time = main_slot

    total_slots = [(0, time, bandwidth)]
    slot_rects = get_next_slot(unavailable_slots, total_slots)
    slot_areas = compute_slot_areas(slot_rects)
    request_areas = r_sorted_by_area(request_r)

    result_texts, allocations, total_available_area, total_r_area = \
        find_r_slot_with_allocation(
            request_areas, slot_areas, slot_rects, unavailable_slots, request_r, time, bandwidth
        )

    is_valid, _ = check_allocation_validity(allocations, main_slot, request_r)

    if is_valid:
        for x1, x2, y1, y2, rid in allocations:
            size, priority = request_r[rid - 1]
            print(f"{{rule_id: {rid}, t_start: {x1:.1f}, t_end: {x2:.1f}, bw_start: {y1:.2f}, bw_end: {y2:.2f}}}")
        print()
        visualize_integrated_schedule(request_r, unavailable_slots, total_slots, allocations)
        return allocations, [], 1

    allocations, dropped_indices, iterations = run_find_least_waste_v2(
        unavailable_slots, main_slot, request_r
    )
    for x1, x2, y1, y2, rid in allocations:
        size, priority = request_r[rid - 1]
        print(f"{{rule_id: {rid}, t_start: {x1:.1f}, t_end: {x2:.1f}, bw_start: {y1:.2f}, bw_end: {y2:.2f}}}")

    if dropped_indices:
        print(f"\nDropped rules: {[idx + 1 for idx in dropped_indices]}")
    print(f"Total iterations: {iterations}\n")

    return allocations, dropped_indices, iterations


def visualize_integrated_schedule(rule_r, unavailable_slots, total_slots, allocations):
    """
    visualize （mostly the same of the one in the warehouse.ipynb）
    """
    fig, ax = plt.subplots(figsize=(12, 8))
    max_time = 0
    if unavailable_slots:
        max_time = max(max_time, max(end for _, end, _ in unavailable_slots))
    if allocations:
        max_time = max(max_time, max(x2 for x1, x2, _, _, _ in allocations))
    max_time = max(max_time, 30)  # Minimum time range

    total_bandwidth = next(iter(total_slots))[2] if total_slots else 100

    for x1, x2, h in total_slots:
        ax.add_patch(patches.Rectangle((x1, 0), x2 - x1, h, fill=False, edgecolor='black', linewidth=2))

    for x1, x2, h in unavailable_slots:
        ax.add_patch(patches.Rectangle((x1, 0), x2 - x1, h, color='red', alpha=0.6, label="Unavailable"))
        ax.text((x1 + x2) / 2, h / 2, f'Reserved\n{h} BW', ha='center', va='center', fontsize=8, color='white',
                weight='bold')

    for x1, x2, y1, y2, rid in allocations:
        ax.add_patch(patches.Rectangle((x1, y1), x2 - x1, y2 - y1, color='blue', alpha=0.7))

        size, priority = rule_r[rid - 1]
        height = y2 - y1
        width = x2 - x1
        label = f'R{rid}\nSize: {size}\nPrio: {priority}\nBW: {height:.1f}'
        ax.text(x1 + width / 2, y1 + height / 2, label, ha='center', va='center', fontsize=8, color='white',
                weight='bold')

    info_x = max_time + 2
    info_y = total_bandwidth - 5
    dy = total_bandwidth / 15
    ax.text(info_x, info_y, "rule (size, priority, height):", fontsize=10, weight='bold')
    info_y -= dy

    rid_to_height = {}
    for x1, x2, y1, y2, rid in allocations:
        height = y2 - y1
        rid_to_height[rid] = height

    for i, (size, priority) in enumerate(rule_r, 1):
        height = f"{rid_to_height.get(i, 0):.1f}" if i in rid_to_height else "0"
        ax.text(info_x, info_y, f"R{i}: ({size}, {priority}, {height})", fontsize=9)
        info_y -= dy

    ax.set_xlim(0, max_time + 8)
    ax.set_ylim(0, total_bandwidth + 10)
    ax.set_xlabel('Time', fontsize=12)
    ax.set_ylabel('Bandwidth', fontsize=12)
    ax.grid(True, alpha=0.3)

    handles = []
    if unavailable_slots:
        handles.append(patches.Patch(color='red', alpha=0.6, label='Unavailable/Reserved'))
    if allocations:
        handles.append(patches.Patch(color='blue', alpha=0.7, label='Allocated rule'))
    if handles:
        ax.legend(handles=handles, loc='upper right')

    plt.tight_layout()