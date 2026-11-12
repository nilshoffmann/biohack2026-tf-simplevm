terraform {
  required_version = ">= 1.5"

  required_providers {
    simplevm = {
      # Not yet published to the registries. Build it locally with
      # `make provider`; see README.md.
      source = "deNBI/simplevm"
    }
  }
}
