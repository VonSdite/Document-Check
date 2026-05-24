import base64
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from flask import Flask

from app.saml1 import process_saml1_response


class Saml1ResponseTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            AUTH={
                "mode": "saml1",
                "saml1": {
                    "acs_url": "https://doc.example.com/auth/saml1/acs",
                    "idp_issuer": "company-saml1",
                    "idp_sso_url": "https://sso.example.com/saml1",
                    "idp_x509_cert": "test-cert",
                    "audience": "https://doc.example.com/saml1",
                    "user_id_attribute": "uid",
                    "username_attribute": "displayName",
                },
            }
        )

    def test_process_saml1_response_extracts_configured_attributes(self):
        response_xml = _saml1_response_xml()
        encoded = base64.b64encode(response_xml.encode("utf-8")).decode("ascii")

        with self.app.test_request_context(
            "/auth/saml1/acs",
            method="POST",
            data={"SAMLResponse": encoded},
        ):
            with patch("app.saml1._validate_signature"):
                user_id, username = process_saml1_response()

        self.assertEqual(user_id, "100086")
        self.assertEqual(username, "张三")


def _saml1_response_xml() -> str:
    not_before = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    not_on_or_after = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""
<samlp:Response
    xmlns:samlp="urn:oasis:names:tc:SAML:1.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:1.0:assertion"
    Recipient="https://doc.example.com/auth/saml1/acs">
  <samlp:Status>
    <samlp:StatusCode Value="samlp:Success" />
  </samlp:Status>
  <saml:Assertion AssertionID="ASSERT-1" Issuer="company-saml1">
    <saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_on_or_after}">
      <saml:AudienceRestrictionCondition>
        <saml:Audience>https://doc.example.com/saml1</saml:Audience>
      </saml:AudienceRestrictionCondition>
    </saml:Conditions>
    <saml:AuthenticationStatement>
      <saml:Subject>
        <saml:NameIdentifier>fallback-name-id</saml:NameIdentifier>
      </saml:Subject>
    </saml:AuthenticationStatement>
    <saml:AttributeStatement>
      <saml:Subject>
        <saml:NameIdentifier>fallback-name-id</saml:NameIdentifier>
      </saml:Subject>
      <saml:Attribute AttributeName="uid">
        <saml:AttributeValue>100086</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute AttributeName="displayName">
        <saml:AttributeValue>张三</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>
"""
