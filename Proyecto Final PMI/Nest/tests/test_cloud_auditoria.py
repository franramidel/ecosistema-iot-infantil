"""SCRUM-20 en modo Supabase: la forma de las consultas que arma Cloud.

No contacta con Supabase. Las reglas de la base se prueban con tests/postgres/.
"""
import unittest
from urllib.parse import parse_qs, urlsplit

from cuna.auditoria import consulta
from cuna.cloud import Cloud
from cuna.common import Problem


class CloudFalso(Cloud):
    def __init__(self, role="admin"):
        super().__init__("https://ejemplo.supabase.co", "pub", "sec")
        self.role, self.calls = role, []

    def access(self, ctx):
        return "token"

    def request(self, path, data=None, token=None, admin=False):
        self.calls.append((path, data))
        table = urlsplit(path).path.rsplit("/", 1)[-1]
        if table == "members":
            return [{"id": "u", "family_id": "f", "role": self.role}]
        if table in ("children", "devices"):
            valor = parse_qs(urlsplit(path).query).get("id", ["eq.x"])[0][3:]
            return [{"id": valor}]
        return [{"id": str(i), "seq": i, "occurred_at": f"2026-11-13T12:00:0{i}+00:00", "event_type": "LECTURA"} for i in range(1, 4)]


class AuditoriaNube(unittest.TestCase):
    def query(self, cloud):
        path = [p for p, _ in cloud.calls if "audit_log" in p][-1]
        return {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}

    def test_filtros_se_traducen_a_postgrest(self):
        cloud = CloudFalso()
        filtros, after, limite = consulta({"child": ["3f454892-3ed9-4db3-87a8-6d939f76c84a"], "device": ["TEST-1"],
                                           "tipo": ["LECTURA"], "desde": ["2026-11-01"], "hasta": ["2026-11-30"], "limit": ["2"]})
        pagina = cloud.audit_page({"id": "u"}, filtros, after, limite)
        q = self.query(cloud)
        self.assertEqual(q["child_id"], "eq.3f454892-3ed9-4db3-87a8-6d939f76c84a")
        self.assertEqual(q["device_id"], "eq.TEST-1")
        self.assertEqual(q["event_type"], "eq.LECTURA")
        self.assertEqual(q["and"], "(event_date.gte.2026-11-01,event_date.lte.2026-11-30)")
        self.assertEqual((q["order"], q["limit"]), ("occurred_at.asc,seq.asc", "3"))
        self.assertEqual(len(pagina["items"]), 2)
        self.assertNotIn("seq", pagina["items"][0])
        self.assertEqual(pagina["next"], "2026-11-13T12:00:02+00:00|2")

    def test_cursor_usa_instante_y_orden_de_insercion(self):
        cloud = CloudFalso()
        filtros, after, limite = consulta({"after": ["2026-11-13T12:00:02+00:00|2"]})
        cloud.audit_page({"id": "u"}, filtros, after, limite)
        q = self.query(cloud)
        self.assertIn('occurred_at.gt."2026-11-13T12:00:02.000000+00:00"', q["or"])
        self.assertTrue(q["or"].endswith("seq.gt.2))"))

    def test_cuidador_no_consulta(self):
        with self.assertRaises(Problem) as ctx:
            CloudFalso("caregiver").audit_page({"id": "u"}, {}, None, 10)
        self.assertEqual(ctx.exception.status, 403)

    def test_evento_de_dispositivo_va_por_la_funcion_de_la_base(self):
        cloud = CloudFalso()
        cloud.record("TEST-1", "clave", ("uuid", "2026-11-13T12:00:00+00:00", "ALERTA", "detalle", "info"))
        path, body = cloud.calls[-1]
        self.assertTrue(path.endswith("/rpc/cuna_record"))
        self.assertEqual(body["kind"], "ALERTA")
        self.assertNotIn("child_id", body)


if __name__ == "__main__":
    unittest.main()
