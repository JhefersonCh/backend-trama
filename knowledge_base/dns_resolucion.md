# DNS: resolución de nombres

## Para qué sirve

DNS traduce nombres como un sitio web a direcciones IP. Si falla, la red puede funcionar (el ping a una IP responde) pero nada se abre por nombre. Especificación base: RFC 1034 y RFC 1035.

## Cómo reconocer un fallo de DNS

- El ping a una IP pública funciona, pero el ping al nombre falla.
- El navegador muestra que no encuentra el servidor.
- Un sitio abre escribiendo su IP pero no por nombre.

## Comprobaciones seguras

1. Ver qué servidores DNS usa el equipo.
2. Resolver un nombre con nslookup o dig contra el DNS configurado.
3. Repetir contra otro DNS autorizado: si este responde, el DNS original es el problema.
4. Revisar si hay proxy configurado en el sistema o el navegador.

## Causas habituales

Servidor DNS caído o inalcanzable, DNS equivocado entregado por DHCP, caché local con datos viejos, proxy mal configurado o filtrado por política de la organización.

## Qué no hacer

No cambiar el DNS de un equipo corporativo por uno público sin autorización: puede romper nombres internos y violar políticas de seguridad.

## Fuentes

RFC 1034 - https://www.rfc-editor.org/rfc/rfc1034 ; RFC 1035 - https://www.rfc-editor.org/rfc/rfc1035
