"""Reading the SP2 schematic's connector tables from word boxes, and comparing with the registry."""

import pytest

from zlscan.hardware import schematic_pins as sp
from zlscan.hardware.pins import PinRegistry

NEW = "V1.3/V1.3B"
OLD = "V1.0/V1.1"


def number(value, x, y):
    return sp.Word(x=x, y=y, text=str(value))


def name(text, x, y):
    return sp.Word(x=x, y=y, text=text)


def left_table_rows(pairs, y0=82.0, pitch=8.0):
    """Words for the left J5 table: ``pairs`` is [(even_pin, even_name, odd_pin, odd_name)].

    Inside numbers sit at x=146 (even) and x=252 (odd); the same numbers are printed again outside
    at x=165 and x=233, 4 pt lower; a name is level with its inside number.
    """
    words = []
    for i, (even, even_name, odd, odd_name) in enumerate(pairs):
        y = y0 + pitch * i
        words += [number(even, 146, y), number(odd, 252, y), number(even, 165, y + 4)]
        words += [number(odd, 233, y + 4)]
        if even_name:
            words.append(name(even_name, 83, y - 1))
        if odd_name:
            words.append(name(odd_name, 278, y - 1))
    return words


def test_bbox_html_is_parsed_into_words():
    html = (
        '<word xMin="83.0" yMin="81.5" xMax="120" yMax="90">GPIO_G16_L4N</word>\n'
        '<word xMin="146" yMin="82" xMax="152" yMax="90">40</word>'
    )
    words = sp.parse_bbox_words(html)
    assert [w.text for w in words] == ["GPIO_G16_L4N", "40"]
    assert (words[0].x, words[0].y) == (83.0, 81.5)


def test_names_pair_with_the_pin_number_on_their_row():
    words = left_table_rows(
        [(40, "GPIO_G16_L4N", 39, "GPIO_G15_L4P"), (38, "GPIO_F16_L1P", 37, "GPIO_E16_L1N")]
    )
    reading = sp.read_table(words, sp.REGIONS[NEW + " J5"])
    assert reading.names == {
        40: "GPIO_G16_L4N", 39: "GPIO_G15_L4P", 38: "GPIO_F16_L1P", 37: "GPIO_E16_L1N",
    }  # fmt: skip
    assert reading.unpaired == [] and reading.ambiguous == []


def test_a_pin_without_a_name_is_simply_absent():
    words = left_table_rows([(6, None, 5, None), (8, "GPIO_H20_L19N", 7, "GPIO_H19_L19P")])
    reading = sp.read_table(words, sp.REGIONS[NEW + " J5"])
    assert set(reading.names) == {8, 7}


def test_the_duplicate_outside_numbers_do_not_confuse_the_pairing():
    words = left_table_rows([(10, "GPIO_F18_L5P", 9, "GPIO_E18_L5N")])
    assert sp.read_table(words, sp.REGIONS[NEW + " J5"]).names == {
        10: "GPIO_F18_L5P",
        9: "GPIO_E18_L5N",
    }


def test_a_power_name_far_from_any_number_stays_unpaired():
    words = left_table_rows([(40, "GPIO_G16_L4N", 39, "GPIO_G15_L4P")]) + [name("5V_DC", 83, 65)]
    reading = sp.read_table(words, sp.REGIONS[NEW + " J5"])
    assert reading.unpaired == ["5V_DC"]
    assert reading.names == {40: "GPIO_G16_L4N", 39: "GPIO_G15_L4P"}


def test_tokens_of_another_table_are_ignored():
    stray = [number(36, 523, 104), name("GPIO_G16_L4N", 441, 104)]  # right table's x range
    words = left_table_rows([(8, "GPIO_H20_L19N", 7, "GPIO_H19_L19P")]) + stray
    reading = sp.read_table(words, sp.REGIONS[NEW + " J5"])
    assert set(reading.names) == {8, 7}


def test_the_side_of_a_name_decides_the_parity_of_its_pin():
    # A name left of the numbers belongs to the even pin even if the odd number is closer in y.
    words = [number(40, 146, 82), number(39, 252, 83), name("GPIO_G16_L4N", 83, 82.5)]
    assert sp.read_table(words, sp.REGIONS[NEW + " J5"]).names == {40: "GPIO_G16_L4N"}


def test_two_different_names_on_one_pin_are_reported_as_ambiguous():
    words = [number(40, 146, 82), name("GPIO_A1_L1P", 83, 81), name("GPIO_B2_L2P", 83, 83)]
    reading = sp.read_table(words, sp.REGIONS[NEW + " J5"])
    assert reading.ambiguous == [40]


@pytest.mark.parametrize(
    "text,role,pin",
    [
        ("GPIO_G16_L4N", "GPIO", "G16"),
        ("GPIO_AA14_L22N", "GPIO", "AA14"),
        ("5V_DC", "5V_DC", ""),
        ("VCC3V3", "VCC3V3", ""),
    ],
)
def test_net_names_are_split_into_role_and_package_pin(text, role, pin):
    assert sp.split_net_name(text) == (role, pin)


def test_an_unrecognised_net_name_is_rejected():
    with pytest.raises(ValueError):
        sp.split_net_name("HDMI_CLK_P")


# ---- comparison with the registry -----------------------------------------------------------


def registry_rows(*rows):
    from zlscan.hardware.pins import PinRow

    return [
        PinRow(
            board_revision=NEW, connector="J5", physical_pin=pin, logical_index=idx,
            package_pin=pp, net_role=role, bank=35, vccio="3.3V", source="t", adc_assignment="u",
        )
        for pin, idx, pp, role in rows
    ]  # fmt: skip


def test_matching_gpio_is_confirmed_and_a_wrong_package_pin_is_a_mismatch():
    rows = registry_rows((7, 0, "H19", "GPIO"), (8, 1, "H21", "GPIO"))
    reading = sp.TableReading(
        names={7: "GPIO_H19_L19P", 8: "GPIO_H20_L19N"}, unpaired=[], ambiguous=[]
    )
    results = {r.physical_pin: r.status for r in sp.compare_table(rows, reading)}
    assert results == {7: sp.RowStatus.CONFIRMED, 8: sp.RowStatus.MISMATCH}


def test_a_ground_pin_without_a_name_is_consistent_but_not_confirmed_by_text():
    rows = registry_rows((5, None, "", "GND"))
    (result,) = sp.compare_table(rows, sp.TableReading({}, [], []))
    assert result.status is sp.RowStatus.GND_SYMBOL_NO_TEXT


def test_a_ground_pin_that_has_a_signal_name_is_a_mismatch():
    rows = registry_rows((5, None, "", "GND"))
    (result,) = sp.compare_table(rows, sp.TableReading({5: "GPIO_H19_L19P"}, [], []))
    assert result.status is sp.RowStatus.MISMATCH


def test_a_power_pin_with_a_misplaced_name_on_the_page_is_flagged_for_a_visual_check():
    rows = registry_rows((40, None, "", "5V_DC"))
    (result,) = sp.compare_table(rows, sp.TableReading({}, ["5V_DC"], []))
    assert result.status is sp.RowStatus.POWER_NAME_NOT_ALIGNED


def test_a_gpio_pin_with_no_name_is_unexplained():
    rows = registry_rows((7, 0, "H19", "GPIO"))
    (result,) = sp.compare_table(rows, sp.TableReading({}, [], []))
    assert result.status is sp.RowStatus.UNEXPLAINED


# ---- XDC ------------------------------------------------------------------------------------

XDC = """
  set_property PACKAGE_PIN H19 [get_ports {J5[0]}];  #IO_B35_LP19
  set_property PACKAGE_PIN U22 [get_ports {J6[0]}];  #J6/1 = IO_B33_LN2
  set_property PACKAGE_PIN Y21 [get_ports {J6[4]}];  #J6/5 = IO_B33_LN9  set_property X Y [R]
  set_property IOSTANDARD LVCMOS33 [get_ports {J5[*]}]
  set_property PACKAGE_PIN M21 [get_ports {hdmi_d_p[0]}]
"""


def test_xdc_entries_are_extracted_with_the_optional_physical_pin_comment():
    entries = sp.parse_xdc(XDC)
    assert [(e.connector, e.logical_index, e.package_pin, e.physical_hint) for e in entries] == [
        ("J5", 0, "H19", None),
        ("J6", 0, "U22", 1),
        ("J6", 4, "Y21", 5),
    ]


def test_xdc_comparison_against_the_real_registry_finds_a_consistent_mapping_or_says_why():
    registry = PinRegistry.load()
    results = sp.compare_xdc(registry, sp.parse_xdc(XDC))
    by = {(r.revision, r.connector, r.logical_index): r.status for r in results}
    assert by[(NEW, "J5", 0)] is sp.XdcStatus.CONFIRMED
    assert by[(OLD, "J5", 0)] is sp.XdcStatus.CONFIRMED  # same logical order in both revisions
    assert by[(NEW, "J6", 0)] is sp.XdcStatus.CONFIRMED
    assert by[(NEW, "J5", 1)] is sp.XdcStatus.NOT_IN_XDC  # this tiny XDC does not list it


def test_xdc_physical_comment_that_disagrees_with_the_registry_is_a_mismatch():
    registry = PinRegistry.load()
    wrong = "set_property PACKAGE_PIN U22 [get_ports {J6[0]}];  #J6/7 = IO_B33_LN2\n"
    results = sp.compare_xdc(registry, sp.parse_xdc(wrong))
    status = {(r.revision, r.connector, r.logical_index): r.status for r in results}
    assert status[(NEW, "J6", 0)] is sp.XdcStatus.MISMATCH


def test_comments_printed_on_later_lines_are_not_attributed_to_an_earlier_port():
    """Page 12 prints the #J6/2..4 comments as a block below the J6[1..3] lines."""
    block = (
        "  set_property PACKAGE_PIN W22 [get_ports {J6[2]}];\n"
        "  set_property PACKAGE_PIN V22 [get_ports {J6[3]}];\n"
        "  #J6/3 = IO_B33_LN3\n"
        "  #J6/4 = IO_B33_LP3\n"
    )
    assert [(e.logical_index, e.physical_hint) for e in sp.parse_xdc(block)] == [
        (2, None),
        (3, None),
    ]


def test_a_power_name_2_4_points_off_its_pin_number_is_not_trusted():
    """Old J5 top: VCC3V3 at y=79.8 beside pin 40 at y=82.2 is stacked among symbols."""
    words = [number(40, 504.5, 82.2), number(38, 504.5, 90.4), name("VCC3V3", 430.5, 79.8)]
    reading = sp.read_table(words, sp.REGIONS[OLD + " J5"])
    assert reading.names == {} and reading.unpaired == ["VCC3V3"]
