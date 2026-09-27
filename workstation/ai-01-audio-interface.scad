// REV enclosure -- AI-01 Audio Interface
// Board rev B (114.00 x 55.00 mm). Units: mm.
// Coordinates: origin = inside floor, bottom-left corner of the cavity; x right, y back, z up.
//
// Edit and save -- REV captures every change.
// Each saved change is diffed, turned into engineering actions, verified, and recorded in GBrain.

// === PARAMETERS (edit these) ===
wall = 2.00;              // wall + floor thickness
standoff_h = 5.00;        // PCB sits on top of the standoffs
cavity_w = 117.00;          // inside width  (x)
cavity_d = 58.00;          // inside depth  (y)
cavity_h = 30.10;          // inside height (z, open top)
board_origin_x = 1.50;     // PCB bottom-left corner in the cavity
board_origin_y = 1.50;

// standoff under mounting hole H1
standoff_H1_x = 5.00;
standoff_H1_y = 5.00;
standoff_H1_dia = 5.50;
standoff_H1_bore = 1.70;

// standoff under mounting hole H2
standoff_H2_x = 112.00;
standoff_H2_y = 5.00;
standoff_H2_dia = 5.50;
standoff_H2_bore = 1.70;

// standoff under mounting hole H3
standoff_H3_x = 112.00;
standoff_H3_y = 53.00;
standoff_H3_dia = 5.50;
standoff_H3_bore = 1.70;

// standoff under mounting hole H4
standoff_H4_x = 5.00;
standoff_H4_y = 53.00;
standoff_H4_dia = 5.50;
standoff_H4_bore = 1.70;

// right wall cut-out for J1 barrel (connector body 9.00 x 11.00); pos = along the wall, z = centre height
opening_J1_barrel_pos = 16.50;
opening_J1_barrel_z = 12.10;
opening_J1_barrel_w = 10.60;
opening_J1_barrel_h = 12.60;

// front wall cut-out for J2 usb_c (connector body 8.94 x 3.26); pos = along the wall, z = centre height
opening_J2_usb_c_pos = 13.00;
opening_J2_usb_c_z = 8.23;
opening_J2_usb_c_w = 9.74;
opening_J2_usb_c_h = 4.06;

// front wall cut-out for J3 usb_a (connector body 13.14 x 5.72); pos = along the wall, z = centre height
opening_J3_usb_a_pos = 64.50;
opening_J3_usb_a_z = 9.46;
opening_J3_usb_a_w = 15.34;
opening_J3_usb_a_h = 7.92;
// === END PARAMETERS ===

// --- PCB reference (read-only, from the board design; drawn with % so it is not part of the part) ---
board_w = 114.00;
board_d = 55.00;
board_t = 1.60;
board_max_component_h = 20.50;

$fn = 48;

module cutout(side, pos, z, w, h) {
    if (side == "front") translate([pos - w/2, -wall - 1, z - h/2]) cube([w, wall + 2, h]);
    if (side == "back")  translate([pos - w/2, cavity_d - 1, z - h/2]) cube([w, wall + 2, h]);
    if (side == "left")  translate([-wall - 1, pos - w/2, z - h/2]) cube([wall + 2, w, h]);
    if (side == "right") translate([cavity_w - 1, pos - w/2, z - h/2]) cube([wall + 2, w, h]);
}

module standoff(x, y, dia, bore) {
    translate([x, y, -0.01]) difference() {
        cylinder(h = standoff_h + 0.01, d = dia);
        translate([0, 0, 1]) cylinder(h = standoff_h + 1, d = bore);
    }
}

module shell() {
    difference() {
        translate([-wall, -wall, -wall]) cube([cavity_w + 2*wall, cavity_d + 2*wall, cavity_h + wall]);
        cube([cavity_w, cavity_d, cavity_h + 1]);   // cavity, open top
        cutout("right", opening_J1_barrel_pos, opening_J1_barrel_z, opening_J1_barrel_w, opening_J1_barrel_h);
        cutout("front", opening_J2_usb_c_pos, opening_J2_usb_c_z, opening_J2_usb_c_w, opening_J2_usb_c_h);
        cutout("front", opening_J3_usb_a_pos, opening_J3_usb_a_z, opening_J3_usb_a_w, opening_J3_usb_a_h);
    }
}

module enclosure() {
    union() {
        shell();
        standoff(standoff_H1_x, standoff_H1_y, standoff_H1_dia, standoff_H1_bore);
        standoff(standoff_H2_x, standoff_H2_y, standoff_H2_dia, standoff_H2_bore);
        standoff(standoff_H3_x, standoff_H3_y, standoff_H3_dia, standoff_H3_bore);
        standoff(standoff_H4_x, standoff_H4_y, standoff_H4_dia, standoff_H4_bore);
    }
}

module pcb() {
    translate([board_origin_x, board_origin_y, standoff_h]) {
        cube([board_w, board_d, board_t]);
        translate([board_w - 7, 15.00 - 9.00/2, 1.60]) cube([8, 9.00, 11.00]);   // J1 barrel
        translate([11.50 - 8.94/2, -1, 1.60]) cube([8.94, 8, 3.26]);   // J2 usb_c
        translate([63.00 - 13.14/2, -1, 1.60]) cube([13.14, 8, 5.72]);   // J3 usb_a
    }
}

enclosure();
%pcb();
