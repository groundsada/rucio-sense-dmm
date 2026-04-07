from _pytest import unittest

import unittest


from least_waste_cleaned_full_version_3 import final_allocation


# ==========================================
# Unit Test Suite
# ==========================================
class TestBandwidthAllocation(unittest.TestCase):

    def assert_allocations_match(self, result, expected):
        self.assertEqual(len(result), len(expected),
                         f"Allocations count mismatch. Got {len(result)}, expected {len(expected)}")

        normalized_result = []
        for item in result:
            if isinstance(item, dict):
                normalized_result.append(item)
            elif isinstance(item, (list, tuple)):
                # (t_start, t_end, bw_start, bw_end, rule_id)
                normalized_result.append({
                    't_start': item[0],
                    't_end': item[1],
                    'bw_start': item[2],
                    'bw_end': item[3],
                    'rule_id': item[4]
                })
            else:
                raise ValueError(f"Unknown item format: {type(item)}")

        result_sorted = sorted(normalized_result, key=lambda x: x['rule_id'])
        expected_sorted = sorted(expected, key=lambda x: x['rule_id'])

        for res, exp in zip(result_sorted, expected_sorted):
            self.assertEqual(res['rule_id'], exp['rule_id'], f"Rule ID mismatch in set: {expected}")

            self.assertAlmostEqual(res['t_start'], exp['t_start'], delta=0.05,
                                   msg=f"t_start mismatch for id {res['rule_id']}")
            self.assertAlmostEqual(res['t_end'], exp['t_end'], delta=0.05,
                                   msg=f"t_end mismatch for id {res['rule_id']}")
            self.assertAlmostEqual(res['bw_start'], exp['bw_start'], delta=0.05,
                                   msg=f"bw_start mismatch for id {res['rule_id']}")
            self.assertAlmostEqual(res['bw_end'], exp['bw_end'], delta=0.05,
                                   msg=f"bw_end mismatch for id {res['rule_id']}")

    def test_01_full_bandwidth_reserved(self):
        """Full bandwidth reserved at start"""
        rules = [(100, 5), (100, 2), (100, 1)]
        main_slot = (100, 60)  # (bw, time)
        reservations = [(0, 20, 100)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)
        self.assert_allocations_match(allocations, [])

    def test_02_many_small_rules(self):
        """Many small rules"""
        rules = [(5, 3), (3, 2), (2, 1), (4, 4), (1, 5)]
        main_slot = (100, 60)
        reservations = [(15, 25, 30)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)
        expected = [
            {'rule_id': 5, 't_start': 0.0, 't_end': 0.0, 'bw_start': 0.00, 'bw_end': 33.33},
            {'rule_id': 3, 't_start': 0.0, 't_end': 0.3, 'bw_start': 33.33, 'bw_end': 40.00},
            {'rule_id': 2, 't_start': 0.0, 't_end': 0.2, 'bw_start': 40.00, 'bw_end': 53.33},
            {'rule_id': 4, 't_start': 0.0, 't_end': 0.1, 'bw_start': 53.33, 'bw_end': 80.00},
            {'rule_id': 1, 't_start': 0.0, 't_end': 0.2, 'bw_start': 80.00, 'bw_end': 100.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_03_general_test(self):
        """General test"""
        rules = [(500, 10), (200, 8), (50, 3), (300, 6), (100, 1)]
        main_slot = (100, 60)
        reservations = [(10, 25, 40), (35, 45, 30)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)
        expected = [
            {'rule_id': 5, 't_start': 0.0, 't_end': 10.0, 'bw_start': 0.00, 'bw_end': 10.00},
            {'rule_id': 4, 't_start': 0.0, 't_end': 8.75, 'bw_start': 10.00, 'bw_end': 44.29},
            {'rule_id': 3, 't_start': 0.0, 't_end': 6.3, 'bw_start': 44.29, 'bw_end': 52.24},
            {'rule_id': 2, 't_start': 0.0, 't_end': 9.4, 'bw_start': 52.24, 'bw_end': 73.47},
            {'rule_id': 1, 't_start': 0.0, 't_end': 18.8, 'bw_start': 73.47, 'bw_end': 100.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_04_reservation_start_zero(self):
        """Reservation starting at t=0"""
        rules = [(100, 5), (100, 2)]
        main_slot = (100, 60)
        reservations = [(0, 60, 50)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)
        expected = [
            {'rule_id': 1, 't_start': 0.0, 't_end': 2.8, 'bw_start': 50.00, 'bw_end': 85.71},
            {'rule_id': 2, 't_start': 0.0, 't_end': 7.0, 'bw_start': 85.71, 'bw_end': 100.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_05_many_small_rules_complex(self):
        """Many small rules (complex reservations)"""
        rules = [(150, 3), (100, 2), (80, 1)]
        main_slot = (100, 80)
        reservations = [(5, 15, 30), (20, 30, 40), (35, 45, 25), (50, 60, 35)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)
        expected = [
            {'rule_id': 3, 't_start': 0.0, 't_end': 4.8, 'bw_start': 0.00, 'bw_end': 16.67},
            {'rule_id': 2, 't_start': 0.0, 't_end': 3.0, 'bw_start': 16.67, 'bw_end': 50.00},
            {'rule_id': 1, 't_start': 0.0, 't_end': 3.0, 'bw_start': 50.00, 'bw_end': 100.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_06_not_enough_bandwidth(self):
        """Not enough bandwidth for all"""
        rules = [(2000, 5), (2000, 3), (2000, 1), (2000, 2)]
        main_slot = (100, 60)
        reservations = [(10, 20, 50)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)
        expected = [
            {'rule_id': 1, 't_start': 0.0, 't_end': 40.0, 'bw_start': 50.00, 'bw_end': 100.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_07_single_large_rule(self):
        """Single large rule (too big)"""
        rules = [(5000, 10)]
        main_slot = (100, 60)
        reservations = [(10, 20, 30)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)
        self.assert_allocations_match(allocations, [])

    def test_08_identical_rules(self):
        """Identical rules"""
        rules = [(100, 5), (100, 5), (100, 5)]
        main_slot = (100, 60)
        reservations = [(15, 20, 50)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)
        expected = [
            {'rule_id': 1, 't_start': 0.0, 't_end': 3.0, 'bw_start': 0.00, 'bw_end': 33.33},
            {'rule_id': 2, 't_start': 0.0, 't_end': 3.0, 'bw_start': 33.33, 'bw_end': 66.67},
            {'rule_id': 3, 't_start': 0.0, 't_end': 3.0, 'bw_start': 66.67, 'bw_end': 100.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_09_complex_overlap(self):
        """
        Reservations: 3 slots overlapping in time/bw
        """
        rules = [(100, 5), (80, 3), (60, 2)]
        main_slot = (50, 30)
        reservations = [(0, 8, 30), (8, 15, 20), (22, 30, 25)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)

        expected = [
            {'rule_id': 3, 't_start': 0.0, 't_end': 15.0, 'bw_start': 30.00, 'bw_end': 34.00},
            {'rule_id': 2, 't_start': 0.0, 't_end': 13.3, 'bw_start': 34.00, 'bw_end': 40.00},
            {'rule_id': 1, 't_start': 0.0, 't_end': 10.0, 'bw_start': 40.00, 'bw_end': 50.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_10_base_case_no_reservations(self):
        """No reservations"""
        rules = [(200, 3), (150, 2), (100, 1)]
        main_slot = (100, 60)
        reservations = []

        allocations, _, _ = final_allocation(reservations, main_slot, rules)

        expected = [
            {'rule_id': 3, 't_start': 0.0, 't_end': 6.0, 'bw_start': 0.00, 'bw_end': 16.67},
            {'rule_id': 2, 't_start': 0.0, 't_end': 4.5, 'bw_start': 16.67, 'bw_end': 50.00},
            {'rule_id': 1, 't_start': 0.0, 't_end': 4.0, 'bw_start': 50.00, 'bw_end': 100.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_11_single_reservation(self):
        """Simple reservation case"""
        rules = [(80, 1), (120, 2), (100, 3)]
        main_slot = (40, 25)
        reservations = [(10, 15, 20)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)

        expected = [
            {'rule_id': 1, 't_start': 0.0, 't_end': 8.3, 'bw_start': 0.00, 'bw_end': 9.67},
            {'rule_id': 3, 't_start': 0.0, 't_end': 6.7, 'bw_start': 9.67, 'bw_end': 24.67},
            {'rule_id': 2, 't_start': 0.0, 't_end': 7.8, 'bw_start': 24.67, 'bw_end': 40.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_12_rules_fits_perfectly(self):
        rules = [(100, 1), (150, 2), (200, 7)]
        main_slot = (40, 20)
        reservations = [(5, 10, 20), (15, 20, 30)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)

        expected = [
            {'rule_id': 1, 't_start': 0.0, 't_end': 5.0, 'bw_start': 0.00, 'bw_end': 20.00},
            {'rule_id': 2, 't_start': 0.0, 't_end': 15.0, 'bw_start': 20.00, 'bw_end': 30.00},
            {'rule_id': 3, 't_start': 0.0, 't_end': 20.0, 'bw_start': 30.00, 'bw_end': 40.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_13_three_reservations_staggered(self):
        rules = [(100, 5), (80, 3), (60, 2)]
        main_slot = (50, 30)
        reservations = [(4, 6, 30), (12, 16, 20), (24, 28, 25)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)

        expected = [
            {'rule_id': 1, 't_start': 0.0, 't_end': 3.3, 'bw_start': 0.00, 'bw_end': 30.00},
            {'rule_id': 3, 't_start': 0.0, 't_end': 7.5, 'bw_start': 30.00, 'bw_end': 38.00},
            {'rule_id': 2, 't_start': 0.0, 't_end': 6.7, 'bw_start': 38.00, 'bw_end': 50.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_14_complex_inner_reservations(self):
        rules = [(150, 4), (120, 5), (100, 3), (80, 2)]
        main_slot = (60, 40)
        reservations = [(5, 10, 15), (10, 15, 20), (15, 20, 18), (25, 30, 30)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)

        expected = [
            {'rule_id': 4, 't_start': 0.0, 't_end': 5.0, 'bw_start': 0.00, 'bw_end': 16.00},
            {'rule_id': 3, 't_start': 0.0, 't_end': 8.5, 'bw_start': 16.00, 'bw_end': 27.75},
            {'rule_id': 2, 't_start': 0.0, 't_end': 8.0, 'bw_start': 27.75, 'bw_end': 42.67},
            {'rule_id': 1, 't_start': 0.0, 't_end': 8.7, 'bw_start': 42.67, 'bw_end': 60.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_15_priority_drop_check(self):
        rules = [(200, 1), (120, 2), (200, 3)]
        main_slot = (40, 15)
        reservations = [(5, 10, 20)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)

        expected = [
            {'rule_id': 2, 't_start': 0.0, 't_end': 5.0, 'bw_start': 0.00, 'bw_end': 24.00},
            {'rule_id': 3, 't_start': 0.0, 't_end': 12.5, 'bw_start': 24.00, 'bw_end': 40.00},
        ]
        self.assert_allocations_match(allocations, expected)

    def test_16_out_of_range(self):
        rules = [(100, 1), (50, 3), (70, 5), (50, 2), (90, 1), (40, 3)]
        main_slot = (40, 15)
        reservations = [(5, 10, 20)]

        allocations, _, _ = final_allocation(reservations, main_slot, rules)

        expected = [
            {'rule_id': 1, 't_start': 0.0, 't_end': 5.0, 'bw_start': 0.00, 'bw_end': 20.00},
            {'rule_id': 6, 't_start': 0.0, 't_end': 15.0, 'bw_start': 20.00, 'bw_end': 22.67},
            {'rule_id': 2, 't_start': 0.0, 't_end': 15.0, 'bw_start': 22.67, 'bw_end': 26.00},
            {'rule_id': 4, 't_start': 0.0, 't_end': 15.0, 'bw_start': 26.00, 'bw_end': 29.33},
            {'rule_id': 3, 't_start': 0.0, 't_end': 15.0, 'bw_start': 29.33, 'bw_end': 34.00},
            {'rule_id': 5, 't_start': 0.0, 't_end': 15.0, 'bw_start': 34.00, 'bw_end': 40.00},
        ]
        self.assert_allocations_match(allocations, expected)
if __name__ == '__main__':
    unittest.main()