-- Lab / synthetic data only.
INSERT INTO sites (id, name, address_label, description, data_origin) VALUES
 ('10000000-0000-4000-8000-000000000001','Sede Central (lab)','Edificio A - piso 1','Sede simulada principal','lab'),
 ('10000000-0000-4000-8000-000000000002','Sucursal Norte (lab)','Oficina remota','Sede simulada secundaria','lab');

INSERT INTO technicians (id, name, email_or_username, specialty, data_origin) VALUES
 ('20000000-0000-4000-8000-000000000001','Técnico Lab 1','tech.lab1','redes','synthetic'),
 ('20000000-0000-4000-8000-000000000002','Técnico Lab 2','tech.lab2','wifi y seguridad','synthetic');

INSERT INTO network_devices (id, site_id, name, device_type, vendor, model, management_ip, serial_number, status, data_origin) VALUES
 ('30000000-0000-4000-8000-000000000001','10000000-0000-4000-8000-000000000001','rtr-central','router','LabVendor','R-100','10.10.0.1','LAB-0001','active','lab'),
 ('30000000-0000-4000-8000-000000000002','10000000-0000-4000-8000-000000000001','sw-central-01','switch','LabVendor','S-24','10.10.0.2','LAB-0002','active','lab'),
 ('30000000-0000-4000-8000-000000000003','10000000-0000-4000-8000-000000000001','ap-central-01','access_point','LabVendor','AP-5','10.10.0.3','LAB-0003','active','lab'),
 ('30000000-0000-4000-8000-000000000004','10000000-0000-4000-8000-000000000002','rtr-norte','router','LabVendor','R-50','10.20.0.1','LAB-0004','active','lab'),
 ('30000000-0000-4000-8000-000000000005','10000000-0000-4000-8000-000000000002','sw-norte-01','switch','LabVendor','S-8','10.20.0.2','LAB-0005','unknown','lab');

INSERT INTO interfaces (device_id, name, mac_address, ip_address, vlan_id, admin_status, oper_status) VALUES
 ('30000000-0000-4000-8000-000000000001','Gi0/0','02:00:00:00:01:01','10.10.0.1',10,'up','up'),
 ('30000000-0000-4000-8000-000000000001','Gi0/1','02:00:00:00:01:02','10.10.20.1',20,'up','up'),
 ('30000000-0000-4000-8000-000000000001','Gi0/2','02:00:00:00:01:03','192.0.2.2',NULL,'up','down'),
 ('30000000-0000-4000-8000-000000000002','Fa0/1','02:00:00:00:02:01',NULL,10,'up','up'),
 ('30000000-0000-4000-8000-000000000002','Fa0/2','02:00:00:00:02:02',NULL,20,'down','down'),
 ('30000000-0000-4000-8000-000000000003','wlan0','02:00:00:00:03:01','10.10.20.3',20,'up','up'),
 ('30000000-0000-4000-8000-000000000004','Gi0/0','02:00:00:00:04:01','10.20.0.1',10,'up','up'),
 ('30000000-0000-4000-8000-000000000005','Fa0/1','02:00:00:00:05:01',NULL,10,'up','unknown');

INSERT INTO networks (site_id, name, cidr, gateway, dns_servers, vlan_id, data_origin) VALUES
 ('10000000-0000-4000-8000-000000000001','LAN administración','10.10.0.0/24','10.10.0.1','{10.10.0.10}',10,'lab'),
 ('10000000-0000-4000-8000-000000000001','LAN usuarios','10.10.20.0/24','10.10.20.1','{10.10.0.10,1.1.1.1}',20,'lab'),
 ('10000000-0000-4000-8000-000000000002','LAN norte','10.20.0.0/24','10.20.0.1','{10.20.0.10}',10,'lab');
