# The API key is read from SIMPLEVM_API_KEY and never stored in this repository.
# The project falls back to SIMPLEVM_PROJECT_ID when var.project_id is null.
provider "simplevm" {
  project_id = var.project_id
  # endpoint = "https://simplevm.denbi.de/portal/api/" # or SIMPLEVM_ENDPOINT
}
