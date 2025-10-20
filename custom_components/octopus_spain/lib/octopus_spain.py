from datetime import datetime, timedelta, time
import os

from python_graphql_client import GraphqlClient

GRAPH_QL_ENDPOINT = "https://api.oees-kraken.energy/v1/graphql/"
SOLAR_WALLET_LEDGER = "SOLAR_WALLET_LEDGER"
ELECTRICITY_LEDGER = "SPAIN_ELECTRICITY_LEDGER"


class OctopusSpain:
    def __init__(self, email, password):
        self._email = email
        self._password = password
        self._token = None

    async def login(self):
        mutation = """
           mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
              obtainKrakenToken(input: $input) {
                token
              }
            }
        """
        variables = {"input": {"email": self._email, "password": self._password}}

        client = GraphqlClient(endpoint=GRAPH_QL_ENDPOINT)
        response = await client.execute_async(mutation, variables)

        if "errors" in response:
            return False

        self._token = response["data"]["obtainKrakenToken"]["token"]
        return True

    async def accounts(self):
        query = """
             query getAccountNames{
                viewer {
                    accounts {
                        ... on Account {
                            number
                        }
                    }
                }
            }
            """

        headers = {"authorization": self._token}
        client = GraphqlClient(endpoint=GRAPH_QL_ENDPOINT, headers=headers)
        response = await client.execute_async(query)

        return list(map(lambda a: a["number"], response["data"]["viewer"]["accounts"]))

    async def hourly_consumption(self, account: str):

        query = """
            query getMeasurements(
                $account: String!,
                $startAt: DateTime!,
                $endAt: DateTime!
                ) {
                account(accountNumber: $account) {
                    properties {
                    id
                    measurements(
                        first: 1500,
                        utilityFilters: [
                                            {
                                            "electricityFilters": {
                                                "readingDirection": "CONSUMPTION",
                                                "readingFrequencyType": "HOUR_INTERVAL"
                                            }
                                            }
                                        ],
                        startAt: $startAt,
                        endAt: $endAt,
                        timezone: "Etc/GMT"
                    ) {
                        edges {
                        node {
                            value
                            unit
                            ... on IntervalMeasurementType {
                            startAt
                            endAt
                            }
                        }
                        }
                    }
                    }
                }
            }
        """

        today_local = datetime.now(tz).date()
        start_date = today_local - timedelta(days=10)
        start_local = datetime.combine(start_date, time.min, tzinfo=tz)
        end_local = datetime.combine(today_local + timedelta(days=1), time.min, tzinfo=tz)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        def to_utc_iso_z(dt: datetime) -> str:
            return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        variables = {
            "account": account,
            "startAt": to_utc_iso_z(start_utc),
            "endAt": to_utc_iso_z(end_utc)
        }
        headers = {"authorization": self._token}
        client = GraphqlClient(endpoint=GRAPH_QL_ENDPOINT, headers=headers)
        response = await client.execute_async(query, variables)
        props = response.get("data", {}).get("account", {}).get("properties", [])
        if not props:
            return []
        edges = props[0]["measurements"]["edges"]
        measurements = [
            {
                "value": edge["node"]["value"],
                "unit": edge["node"]["unit"],
                "startAt": edge["node"]["startAt"],
                "endAt": edge["node"]["endAt"],
            }
            for edge in edges
        ]
        return measurements
    
    async def account(self, account: str):
        query = """
            query ($account: String!) {
              accountBillingInfo(accountNumber: $account) {
                ledgers {
                  ledgerType
                  statementsWithDetails(first: 1) {
                    edges {
                      node {
                        amount
                        consumptionStartDate
                        consumptionEndDate
                        issuedDate
                      }
                    }
                  }
                  balance
                }
              }
            }
        """
        headers = {"authorization": self._token}
        client = GraphqlClient(endpoint=GRAPH_QL_ENDPOINT, headers=headers)
        response = await client.execute_async(query, {"account": account})
        ledgers = response["data"]["accountBillingInfo"]["ledgers"]
        electricity = next(filter(lambda x: x['ledgerType'] == ELECTRICITY_LEDGER, ledgers), None)
        solar_wallet = next(filter(lambda x: x['ledgerType'] == SOLAR_WALLET_LEDGER, ledgers), {'balance': 0})

        if not electricity:
            raise Exception("Electricity ledger not found")

        invoices = electricity["statementsWithDetails"]["edges"]

        if len(invoices) == 0:
            return {
                'solar_wallet': None,
                'last_invoice': {
                    'amount': None,
                    'issued': None,
                    'start': None,
                    'end': None
                }
            }

        invoice = invoices[0]["node"]

        # Los timedelta son bastante chapuzas, habrá que arreglarlo
        return {
            "solar_wallet": (float(solar_wallet["balance"]) / 100),
            "octopus_credit": (float(electricity["balance"]) / 100),
            "last_invoice": {
                "amount": invoice["amount"] if invoice["amount"] else 0,
                "issued": datetime.fromisoformat(invoice["issuedDate"]).date(),
                "start": (datetime.fromisoformat(invoice["consumptionStartDate"]) + timedelta(hours=2)).date(),
                "end": (datetime.fromisoformat(invoice["consumptionEndDate"]) - timedelta(seconds=1)).date(),
            },
        }
