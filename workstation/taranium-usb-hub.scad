// REV enclosure -- Taranium-USB-Hub
// Board rev B (61.20 x 32.40 mm). Units: mm.
// Coordinates: origin = inside floor, bottom-left corner of the cavity; x right, y back, z up.
//
// Edit and save -- REV captures every change.
// Each saved change is diffed, turned into engineering actions, verified, and recorded in GBrain.

// === PARAMETERS (edit these) ===
wall = 2.00;              // wall + floor thickness
standoff_h = 5.00;        // PCB sits on top of the standoffs
cavity_w = 64.20;          // inside width  (x)
cavity_d = 35.40;          // inside depth  (y)
cavity_h = 23.10;          // inside height (z, open top)
board_origin_x = 1.50;     // PCB bottom-left corner in the cavity
board_origin_y = 1.50;

// standoff under mounting hole H1
standoff_H1_x = 20.70;
standoff_H1_y = 4.70;
standoff_H1_dia = 5.50;
standoff_H1_bore = 2.70;

// standoff under mounting hole H2
standoff_H2_x = 44.70;
standoff_H2_y = 4.70;
standoff_H2_dia = 5.50;
standoff_H2_bore = 2.70;

// standoff under mounting hole H3
standoff_H3_x = 32.10;
standoff_H3_y = 28.90;
standoff_H3_dia = 5.50;
standoff_H3_bore = 2.70;

// left wall cut-out for J3 usb_a (connector body 13.14 x 5.72); pos = along the wall, z = centre height
opening_J3_usb_a_pos = 13.30;
opening_J3_usb_a_z = 9.46;
opening_J3_usb_a_w = 14.14;
opening_J3_usb_a_h = 6.72;

// front wall cut-out for J1 usb_a (connector body 13.14 x 5.72); pos = along the wall, z = centre height
opening_J1_usb_a_pos = 32.10;
opening_J1_usb_a_z = 9.46;
opening_J1_usb_a_w = 14.14;
opening_J1_usb_a_h = 6.72;

// right wall cut-out for J5 usb_c (connector body 8.94 x 3.26); pos = along the wall, z = centre height
opening_J5_usb_c_pos = 27.90;
opening_J5_usb_c_z = 8.23;
opening_J5_usb_c_w = 9.74;
opening_J5_usb_c_h = 4.06;

// left wall cut-out for J4 usb_c (connector body 8.94 x 3.26); pos = along the wall, z = centre height
opening_J4_usb_c_pos = 27.90;
opening_J4_usb_c_z = 8.23;
opening_J4_usb_c_w = 9.74;
opening_J4_usb_c_h = 4.06;

// right wall cut-out for J2 usb_a (connector body 13.14 x 5.72); pos = along the wall, z = centre height
opening_J2_usb_a_pos = 13.30;
opening_J2_usb_a_z = 9.46;
opening_J2_usb_a_w = 14.14;
opening_J2_usb_a_h = 6.72;
// === END PARAMETERS ===

// --- PCB reference (read-only, from the board design; drawn with % so it is not part of the part) ---
board_w = 61.20;
board_d = 32.40;
board_t = 1.60;
board_max_component_h = 13.50;

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
        cutout("left", opening_J3_usb_a_pos, opening_J3_usb_a_z, opening_J3_usb_a_w, opening_J3_usb_a_h);
        cutout("front", opening_J1_usb_a_pos, opening_J1_usb_a_z, opening_J1_usb_a_w, opening_J1_usb_a_h);
        cutout("right", opening_J5_usb_c_pos, opening_J5_usb_c_z, opening_J5_usb_c_w, opening_J5_usb_c_h);
        cutout("left", opening_J4_usb_c_pos, opening_J4_usb_c_z, opening_J4_usb_c_w, opening_J4_usb_c_h);
        cutout("right", opening_J2_usb_a_pos, opening_J2_usb_a_z, opening_J2_usb_a_w, opening_J2_usb_a_h);
    }
}

module enclosure() {
    union() {
        shell();
        standoff(standoff_H1_x, standoff_H1_y, standoff_H1_dia, standoff_H1_bore);
        standoff(standoff_H2_x, standoff_H2_y, standoff_H2_dia, standoff_H2_bore);
        standoff(standoff_H3_x, standoff_H3_y, standoff_H3_dia, standoff_H3_bore);
    }
}

module pcb() {
    translate([board_origin_x, board_origin_y, standoff_h]) {
        cube([board_w, board_d, board_t]);
        translate([-1, 11.80 - 13.14/2, 1.60]) cube([8, 13.14, 5.72]);   // J3 usb_a
        translate([30.60 - 13.14/2, -1, 1.60]) cube([13.14, 8, 5.72]);   // J1 usb_a
        translate([board_w - 7, 26.40 - 8.94/2, 1.60]) cube([8, 8.94, 3.26]);   // J5 usb_c
        translate([-1, 26.40 - 8.94/2, 1.60]) cube([8, 8.94, 3.26]);   // J4 usb_c
        translate([board_w - 7, 11.80 - 13.14/2, 1.60]) cube([8, 13.14, 5.72]);   // J2 usb_a
    }
}

enclosure();
%pcb();
