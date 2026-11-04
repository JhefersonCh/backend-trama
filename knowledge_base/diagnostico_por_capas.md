# Diagnóstico de red por capas

## Idea general

Un problema de conectividad se aísla mejor si se comprueba de abajo hacia arriba y se avanza solo cuando la capa anterior funciona. La primera comprobación que falla señala dónde buscar. Es una guía práctica inspirada en el modelo de capas OSI y en la pila TCP/IP; no reproduce ningún texto de referencia.

## Capa física y enlace

Comprobar que exista enlace: cable bien conectado y luz de enlace, o Wi-Fi asociado a la red correcta. Si la interfaz aparece desconectada, lo más probable es el cable, el puerto o el adaptador. En Wi-Fi conviene mirar la intensidad de señal (RSSI) y la banda usada.

## Capa de red: IP y gateway

Verificar que el equipo tenga una dirección IP válida, la máscara correcta y un gateway dentro de su misma subred. Una dirección 169.254.x.x indica que no se obtuvo concesión DHCP. Luego hacer ping al gateway: si responde, la red local funciona; si no, revisar subred, VLAN y estado del gateway.

## Salida a Internet

Hacer ping a una IP pública conocida. Si responde, el enrutamiento y la salida funcionan. Si no responde pero el gateway sí, el problema está en el router, el NAT, el firewall o el proveedor.

## Resolución de nombres

Intentar resolver un nombre de dominio. Si el ping a la IP funciona pero el nombre no resuelve, el problema es DNS (servidor configurado, caché o proxy). Es la causa más frecuente de "hay red pero no cargan páginas".

## Aplicación

Si todo lo anterior funciona y aún falla un servicio concreto, revisar el proxy, el navegador, los certificados o el propio servicio. Registrar siempre qué se comprobó y el resultado, para poder escalar con evidencia.

## Fuentes

Modelo OSI (ISO/IEC 7498-1) y suite TCP/IP, RFC 1122 - Requirements for Internet Hosts - https://www.rfc-editor.org/rfc/rfc1122
