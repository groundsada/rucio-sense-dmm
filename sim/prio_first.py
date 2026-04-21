def schedule_rules(rules, bandwidth, reservations):
    # check base case (everything fits before the first reservation)
    total_size = sum(size for size, priority in rules)
    total_priority = sum(priority for size, priority in rules)
    if reservations:
        first_res_start = min(start for start, end, res_bw in reservations)
        if total_size / bandwidth <= first_res_start:
            return [(idx, 0, size / (priority / total_priority * bandwidth), priority / total_priority * bandwidth) for idx, (size, priority) in enumerate(rules)]
        else:
            print("Base case does not hold, proceeding with scheduling")

    sorted_rules = sorted(enumerate(rules), key=lambda x: (-x[1][1], x[1][0]))
    
    results = []
    used_bw_at_time_0 = 0
    
    reserved_bw_at_time_0 = sum(res_bw for start_time, end_time, res_bw in reservations 
                               if start_time <= 0 < end_time)
    
    for idx, (size, priority) in sorted_rules:
        available_bw = bandwidth - reserved_bw_at_time_0 - used_bw_at_time_0
        
        if available_bw >= 1:
            alloc_bw = min(available_bw, size)
            duration = size / alloc_bw
            
            results.append((idx, 0, duration, alloc_bw))
            used_bw_at_time_0 += alloc_bw
    
    return results