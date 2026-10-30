# Fortanix CCM Subject Matter Guide

Prepared for Pinnacle research • 28 October 2026

## What the researcher needs to understand

Confidential Computing Manager addresses a specific trust problem: a customer wants useful work performed on sensitive data or proprietary models while limiting what the people operating the infrastructure can access. CCM coordinates verification and policy. Hardware provides the protected execution environment. A key service controls access to the encrypted assets. Understanding those separate responsibilities is essential to understanding the product.

This guide defines the terminology used in the companion CCM profile, explains the workflow, and connects technical choices to customer problems. General definitions describe concepts; they do not assert that every Fortanix configuration implements every feature. Product-specific details retain their source and deployment scope. The inference labels in the profile still apply when using this guide.

## A worked example of protected model deployment

Consider a hypothetical model provider and a hospital. The provider wants its model to run near the hospital's patient data, but does not want the hospital's infrastructure administrators to copy the model weights. The hospital wants useful predictions without sending unrestricted patient records to the provider. This is an illustrative use case, not a named deployment.

1. **Package the workload.** The provider prepares an approved application and encrypted model. The parties agree on permitted processing and outputs. Encryption protects the stored model while the key is withheld.

2. **Launch the protected environment.** A compatible confidential VM or container starts on the selected CPU and, if required, confidential GPU. Ordinary containers alone do not provide the same protection from host administrators.

3. **Produce and verify evidence.** The platform supplies signed attestation evidence. CCM and the relevant verification services evaluate the environment and measured state against policy.

4. **Issue a workload identity.** If the checks pass, CCM issues the appropriate certificate. The workload keeps the corresponding private key protected and uses the certificate to authenticate.

5. **Request authorized material.** In the documented joint design, the workload connects to DSM over mutual TLS. DSM validates the identity and applicable key-access permissions before supplying protected material to the authorized environment.

6. **Run and maintain the workload.** Data and model weights become usable inside the protected boundary. The application produces permitted results. Operators still manage updates, certificate lifecycles, availability, and application security. [S3][S8][S11]

The cryptographic architecture does not by itself decide which hospital employee may view a result, whether a prediction is accurate, or whether an output reveals too much. Those remain application, policy and governance questions.

## Data protection and trust

**Data at rest.** Information stored on a disk, database, object store or backup. Storage encryption protects it while stored; authorized software normally decrypts it to use it.

**Data in transit.** Information moving between systems. TLS commonly protects this channel. Protection ends at the TLS endpoint, so the endpoint's trust boundary matters.

**Data in use.** Information being processed, including values held in working memory. This is the exposure confidential computing is designed to address.

**Plaintext and ciphertext.** Plaintext is data in a directly usable form; ciphertext is its encrypted representation. Inside a protected environment, the application can use plaintext even though surrounding infrastructure is prevented from reading the protected memory.

**Confidential computing.** Computation inside a hardware-backed, attested trusted execution environment to protect data during use. It can apply in clouds or on premises. [T2]

**Trusted Execution Environment or TEE.** A protected execution boundary that provides specified confidentiality and integrity properties against actors outside it. The exact protected assets and excluded attackers depend on the implementation. [T2]

**Enclave.** An isolated execution region for selected code and data. In Intel SGX, an enclave is within an application's address space. Vendors sometimes use the term more broadly; do not assume every enclave is a full VM.

**Confidentiality, integrity and availability.** Confidentiality restricts disclosure; integrity restricts unauthorized alteration; availability concerns whether the service can be used. Strong confidentiality does not stop an infrastructure owner from powering off a machine.

**Threat model.** A statement of what is protected, which adversaries are considered, and what assumptions remain. A claim about privileged host access does not automatically cover malicious approved code or all physical attacks.

**Trust boundary.** The dividing line between components or actors relied upon for security and those treated as potentially hostile. A confidential VM typically trusts more guest software than a narrowly scoped application enclave.

**Trusted Computing Base or TCB.** The hardware, firmware and software whose correct operation is needed for the stated security property. In attestation documentation, a TCB level often describes relevant component security versions; it is not a complete audit of the application.

**Root of trust.** A foundational component or key trusted to anchor verification. Evidence is meaningful only if its signing authority and verification chain are trustworthy.

**Host, guest and hypervisor.** The host supplies infrastructure; the guest is the VM's operating environment; the hypervisor manages VMs and their resources. Confidential-VM technology aims to reduce trust in privileged host software while retaining trust in appropriate guest components. [T3][T5]

**Privileged administrator and insider threat.** An administrator has powerful system permissions; an insider threat involves misuse or compromise of authorized access. CCM's relevant question is which privileges can reach protected assets, not whether all insiders become harmless.

**Attack surface and side channel.** The attack surface is the set of exposed interfaces and behaviors an attacker might exploit. A side channel reveals information indirectly, for example through timing or access patterns. TEE protection does not imply every side channel is eliminated. [T10]

**Zero Trust.** An approach that avoids granting access solely because a requester is inside a network or organization. Attestation can supply one input to an access decision; it does not eliminate trusted components.

**Runtime Encryption.** Fortanix's terminology for protecting sensitive information during execution using confidential-computing techniques. It should be explained through the concrete isolation, verification and key-control mechanisms rather than treated as a separate guarantee. [S1][S3]

## Attestation and verification

**Remote attestation.** A process in which one system supplies evidence about its state for another system to evaluate. It can support a decision to grant access without merely trusting the infrastructure operator's assertion. [T1]

**Attester, verifier and relying party.** The attester produces evidence. The verifier evaluates it. The relying party uses the result to decide what action or access to permit. In the joint Fortanix flow, these functions span workload/platform components, CCM and vendor services, and the key-access decision. [T1][S3]

**Attestation evidence, report or quote.** Signed claims about a platform or execution environment. The exact format and measurements differ by hardware. A valid signature does not mean every claimed configuration is acceptable to the customer.

**Measurement and reference value.** A measurement describes an observed component or state, often using a cryptographic digest. A reference value describes an expected or approved state. Matching a reference establishes consistency with that reference, not that the software is bug-free.

**Cryptographic hash or digest.** A compact value computed from data that is designed to change when the input changes. An image digest can identify an exact build. Hashing is not encryption and does not make the input confidential.

**Attestation policy.** The acceptance rules applied to evidence: approved measurements, versions, hardware or other conditions. The rules determine which measured environments are acceptable.

**Composite attestation.** Evaluating several relevant components together, such as a confidential CPU and GPU, before accepting a workload. Verifying a GPU alone does not establish the security of its CPU environment or application. [S3][T5]

**Freshness, nonce and replay.** Freshness concerns whether evidence is recent enough. A nonce is a challenge value that can bind evidence to a particular exchange. A replay attack reuses an old valid message. Verification must address freshness as well as signatures. [T1]

**Re-attestation.** Obtaining and evaluating new evidence after an earlier verification. It may happen on a lifecycle event or schedule; the exact trigger is a product/configuration question. A heartbeat is not automatically re-attestation.

**Attestation collateral.** Supporting verification material such as manufacturer certificates, reference measurements, security-version information and revocation data. Outdated collateral can affect the assessment of an otherwise correctly signed report.

**Intel DCAP, PCS and PCCS.** Data Center Attestation Primitives support Intel attestation. The Provisioning Certification Service supplies verification collateral; a Provisioning Certification Caching Service caches relevant collateral for deployment use. These are distinct from CCM's customer policy. [S8]

**AMD KDS.** AMD's Key Distribution Service provides certificate material used in validating supported AMD attestation evidence. It is an attestation dependency, not the customer's general-purpose application key manager. [S8]

**Microsoft Azure Attestation or MAA.** Azure's service for evaluating attestation evidence and issuing results. It can participate in a supported CCM verification path; using it does not make every CCM deployment Azure-only. [S8]

**NVIDIA Remote Attestation Service or NRAS.** A service for evaluating NVIDIA device evidence. GPU evidence and verification help establish relevant device identity and state; CPU and application checks remain separate responsibilities. [S8][T5]

**TCB grace period.** A policy window for handling a platform whose security level is affected by a disclosed issue. The reviewed CCM guide allows continuing with a flag or blocking attestation/key release after the applicable deadline. Its default 42 days is a patch-policy value, not a certificate lifetime. [S19][S21]

## Workload identity certificates and keys

**Workload identity.** An identity assigned to software rather than a human user. It can let a key service distinguish approved workloads and apply their permissions.

**Authentication and authorization.** Authentication establishes an identity; authorization decides what that identity may do. Proving that a certificate came from a trusted CA does not by itself authorize every key operation.

**Key pair and digital signature.** A key pair consists of a private key and corresponding public key. A digital signature lets a verifier check that a message was signed using the corresponding private key and has not been altered.

**X.509 certificate.** A signed record binding a public key to identity information and constraints, including a validity interval. A certificate is not the private key and need not contain the decryption key for the workload's data.

**Certificate Authority or CA.** An authority that signs certificates. A certificate chain connects a presented certificate to an issuer the recipient trusts.

**Zone CA.** The CCM-associated issuing authority used in the documented workload-certificate flow. Its certificate is configured as a trust anchor for the relevant key-release integration; it does not imply trust in all Fortanix customers. [S3]

**Certificate Signing Request or CSR.** A request containing a public key and requested identity information for a CA to certify. Sending a CSR is not proof that the requester satisfies attestation or authorization policy.

**TLS and mutual TLS or mTLS.** TLS protects a network connection and authenticates the relevant endpoint. With mTLS, both sides authenticate, commonly using certificates. Fortanix's joint design uses this channel for an identified workload to communicate with DSM. [T7][S3]

**Attested TLS.** TLS whose endpoint identity is bound to attestation evidence or a verified result. The binding matters: encryption to an unintended endpoint can still expose data there.

**Expiry, renewal and revocation.** Expiry ends a certificate's validity interval. Renewal replaces or extends credentials under a defined process. Revocation invalidates a credential before expiry through mechanisms that recipients must enforce. They are not interchangeable.

**Secret.** Sensitive material such as a password, API token, certificate private key or encryption key. Different secrets can require different permissions and lifecycle controls.

**Key Management System or Service or KMS.** A system for managing cryptographic keys and authorized operations throughout their lifecycle. Key custody, use, rotation and release are separate capabilities.

**Hardware Security Module or HSM.** A protected cryptographic module that safeguards keys and performs operations under defined security controls. An HSM is not the same thing as the confidential environment running the customer's application.

**Secure Key Release or SKR.** Providing key material only when the recipient meets defined identity, verification and policy conditions. In the documented Fortanix design, CCM supplies attested identity and DSM participates in the release decision. [S3]

**Key wrapping and non-exportable keys.** Wrapping encrypts one key using another. A non-exportable key is restricted from leaving its owning security boundary in the prohibited form. An architecture may keep a wrapping key in an HSM while releasing a distinct data key to a TEE. Do not assume a solution brief's generic word “key” identifies the same object at every step.

**Rotation, re-encryption and zeroization.** Rotation introduces replacement keys; re-encryption protects data under a new key; zeroization removes sensitive material from a specified location. None retroactively removes plaintext already copied elsewhere.

**Fail closed and fail open.** A fail-closed operation denies access when a required security check cannot complete. A fail-open operation permits it. State which operation is affected: refusing new key requests does not necessarily stop an existing workload.

**FIPS 140 validation.** Validation of a particular cryptographic module against a defined standard and security boundary. The profile's cited DSM FIPS 140-2 Level 3 statement does not certify every CCM deployment or customer application. [S3]

## Deployment vocabulary and platform choices

**Workload, application and build.** A workload is a running unit of useful computation. An application describes the software; a build is a particular packaged version. Approving one build does not establish that every later version should be trusted.

**CPU and GPU.** A CPU executes general-purpose computation and system tasks. A GPU accelerates highly parallel computation, including AI operations. Protecting CPU memory alone does not establish protection when sensitive model computation moves to a GPU.

**Virtual machine or VM and confidential VM or CVM.** A VM runs a guest operating system on virtualized resources. A CVM adds hardware-backed protection from specified host-level access. Its guest OS and applications can still be trusted components.

| **Platform term** | **Meaning** | **Research distinction** |
| --- | --- | --- |
| Intel SGX | Software Guard Extensions protect application enclaves | An application-scoped boundary differs from a whole guest VM |
| Intel TDX | Trust Domain Extensions protect confidential VMs | The protected workload includes a guest software stack |
| AMD SEV-SNP | Secure Encrypted Virtualization with Secure Nested Paging protects supported VMs | Hardware, firmware and guest compatibility still matter |
| AWS Nitro Enclaves | Isolated execution environments created from supported EC2 parent instances | Uses a particular AWS architecture rather than being an SGX synonym |
| NVIDIA Hopper and Blackwell | GPU architecture families used in supported confidential-computing configurations | Family names do not guarantee every model, mode or topology is supported |

Platform definitions follow Intel, AMD, AWS and NVIDIA documentation. CCM compatibility must separately follow its release and deployment guidance. [T3][T4][T5][T6][S10][S19]

**Container, Docker image and registry.** A container packages and runs application processes. An image is its packaged filesystem and configuration; a registry stores and distributes images. Standard container isolation usually relies on host software rather than shielding memory from a privileged host.

**Kubernetes and pod.** Kubernetes schedules and manages container workloads. A pod is its basic deployable group of containers. Running on Kubernetes does not by itself make a workload confidential.

**Confidential Containers or CoCo.** A project that combines container workflows with confidential execution. In Fortanix's described path, a pod uses a confidential VM boundary and an attestation/key-release integration. It is distinct from merely running ordinary containers on an encrypted disk. [S22]

**Runtime class, Helm and CI/CD.** A Kubernetes runtime class selects how a workload runs. Helm packages Kubernetes deployment configuration. Continuous integration and delivery automate build and release work. These tools carry configuration; they do not replace attestation.

**Node and Node Agent.** A node supplies compute resources. Fortanix's Node Agent participates in enrollment, collection or forwarding of evidence, and certificate-related communication for the documented path. Its exact location and role depend on deployment. [S8][S11]

**Enrollment, allowlist and labels.** Enrollment registers a resource. An allowlist specifies accepted items such as builds. Labels attach metadata used in policy decisions. In the reviewed CCM guide, application requirements are compared with node labels during certificate issuance. [S12]

**Manifest, configuration and workflow.** A manifest declares how software should run. Configuration supplies operational settings. A workflow organizes execution steps and dependencies. Their existence does not establish that every deployment supports the same orchestration features.

**Enclave OS and EDP.** Enclave OS is Fortanix technology used in documented application conversion/execution paths. Enclave Development Platform is a developer-oriented enclave toolchain. These are different paths from the broader category of confidential VMs. [S4][S10][S11]

**ACI.** Azure Container Instances, Microsoft's managed container execution service. A CCM entry for an ACI path is scoped to the supported confidential configuration, not a claim that every ACI deployment has the same protections. [S10]

**SaaS, on premises, hybrid and multicloud.** SaaS is vendor-operated software delivered as a service. On premises places software in customer-managed infrastructure. Hybrid combines environments; multicloud uses more than one cloud provider. These terms describe operating models, not guaranteed feature parity.

**Air gap and high availability or HA.** An air gap isolates an environment from designated networks. HA reduces downtime through redundancy and recovery design. Neither follows automatically from an on-premises deployment; attestation dependencies and service topology must be examined.

**IAM and tenant.** Identity and Access Management controls identities and permissions. A tenant is a logically separated customer or organizational context. Human account access and workload key permissions are related but distinct controls.

## AI and collaboration vocabulary

**Model weights and model IP.** Weights are learned numerical parameters used by a model. Model intellectual property can also include code, architecture and training know-how. Protecting a weights file does not protect every form of IP or prevent all extraction through model outputs.

**Inference and training.** In AI systems, inference means running a model to produce outputs; training updates model parameters from data. This differs from the research profile's “inference,” which means a reasoned assumption about the business or product.

**Fine-tuning.** Additional training that adapts an existing model. Its memory, data and hardware requirements can differ from serving inference; support for one does not prove support for the other.

**Embedding and vector database.** An embedding is a numerical representation useful for similarity operations. A vector database stores and searches those representations. Embeddings derived from sensitive content should not be assumed anonymous merely because they are numbers.

**Retrieval augmented generation or RAG.** A system retrieves relevant information and provides it to a generative model as context. Its protection requirements span source documents, retrieval, embeddings, model execution and outputs, not just the model server.

**Data steward, model owner, publisher and consumer.** A steward governs permitted data use. A model owner controls model IP. A publisher supplies an asset or workload; a consumer runs or uses it. A single organization can occupy several roles.

**Private multiparty collaboration.** Several parties perform useful work while restricting exposure of their contributed data or algorithms. In the CCM profile this can use TEEs; it should not be confused with a claim that CCM implements secure multiparty computation.

**Homomorphic encryption and secure multiparty computation.** Homomorphic encryption permits specified computations on encrypted data. Secure multiparty computation distributes a computation among parties while limiting what they learn. They use different assumptions and tradeoffs from hardware TEEs; no universal performance ranking is implied.

**Federated learning.** A training arrangement where participants compute locally and share updates instead of centralizing all raw data. The updates can still carry sensitive information; federated learning and confidential computing can be complementary concepts.

**Sovereignty and data residency.** Residency concerns location. Sovereignty concerns applicable authority and control. Keeping data in one country does not alone determine who can access it, and a TEE does not alone satisfy every sovereignty requirement.

**GDPR and HIPAA references.** GDPR is a European data-protection framework; HIPAA includes US health-information requirements for covered contexts. In this research, these names identify customer constraints and vendor positioning. They are not certifications automatically conferred by CCM.

## Product names and commercial vocabulary

**CCM and DSM.** CCM is Fortanix's confidential-workload management and verification product. DSM is its key-management and cryptographic-services product. Keeping the roles separate prevents attributing every DSM capability to CCM. [S3][S8]

**Confidential AI, Armet AI and Armor.** Confidential AI is the broader solution context. Some sources use Armet AI naming. Armor appears as the platform/account environment in current setup guidance. Names do not establish mandatory bundles or identical feature scope. [S6][S9][S11]

**Champion, economic buyer and approver.** The champion drives adoption; the economic buyer controls the relevant budget; an approver can permit or block the decision. One person may perform several roles. Our proposed CCM role mapping is inference, not recorded deal evidence.

**CISO, CTO and procurement.** A Chief Information Security Officer leads security; a Chief Technology Officer leads technology at company-dependent scope. Procurement manages purchasing and supplier processes. Job titles do not prove authority over a particular project.

**Proof of concept and proof of value.** A proof of concept tests whether a design can work. A proof of value tests whether the resulting capability is useful enough to justify adoption. The same pilot can address both if success criteria include technical and business outcomes.

**TCO and ROI.** Total cost of ownership includes software, infrastructure, integration and operations. Return on investment compares benefits with cost over a stated period. Neither can be calculated from a product's feature list alone.

**SLA and support entitlement.** A service-level agreement defines contractual service commitments. A support entitlement defines the assistance included. A response-time target is not a guaranteed resolution time or uptime percentage. [S24]

**Latency, throughput and overhead.** Latency is time to complete an operation; throughput is work completed per unit time; overhead is additional resource use or time relative to a defined baseline. Startup attestation cost and steady-state inference cost should be measured separately.

**Audit log, tamper evidence and immutable storage.** An audit log records events. Tamper evidence helps detect changes; immutability restricts changes under a particular mechanism. A log does not prove that all relevant events were captured or that a control was effective.

## Distinctions that prevent bad research conclusions

| **If a source says** | **The researcher should distinguish** |
| --- | --- |
| The workload is attested | What state was checked, when, against which policy, and by whom |
| Keys are revoked | Future access denial versus existing copies and running sessions |
| The model stays encrypted | Stored/transferred ciphertext versus usable data inside protected memory |
| No code changes | Application edits versus deployment, policy and operations work |
| On-premises support | The workload location versus the location and availability of the CCM control plane |
| Hardware is supported | A family name versus the exact device, firmware, topology and release |
| FIPS certified | The validated module and boundary versus the whole application |
| The product supports compliance | A technical control versus the customer's complete obligations |

## Applying the subject knowledge to market research

Translate mechanisms into the task someone needs to accomplish. A platform engineer researching attestation may actually need to release model keys only to approved builds. A model provider researching confidential GPUs may need to deploy inside a customer environment without handing over unrestricted weights. A data steward researching secure collaboration may need a way to approve computation without exporting the source dataset.

For each topic, record the actor, task, blocker, relevant mechanism, required conditions, and a credible alternative. For example: a model provider wants local customer deployment; unrestricted administrator access blocks approval; protected CPU/GPU execution plus verified key access is a possible approach; hardware and integration must fit; alternatives include retaining hosted inference or accepting a different trust arrangement. This example is a research hypothesis, not established market demand.

Use the companion profile for Fortanix-specific scope and assumptions. Use this guide to understand terms and formulate better questions. Neither document establishes keyword demand, search intent, competitive difficulty, or an SEO opportunity without the next research stage.

## Sources and evidence scope

S1 to S21 refer to the source pages preserved in the completed Fortanix run. S22 to S25 were reviewed for this update on 28 October 2026. T references support general technical definitions. Sources establish their own statements, not the truth of our commercial hypotheses or universal support across deployments.

**S1**  [Fortanix company homepage](https://www.fortanix.com/)

**S3**  [Attestation gated secure key release architecture](https://www.fortanix.com/blog/securing-enterprise-applications-and-ai-pipelines-with-attestation-gated-secure-key-release)

**S4**  [What is Confidential Computing Manager](https://support.fortanix.com/docs/what-is-fortanix-confidential-computing-manager)

**S6**  [Confidential AI solution page](https://www.fortanix.com/platform/confidential-ai)

**S8**  [CCM on premises architecture](https://support.fortanix.com/docs/fortanix-confidential-computing-manager-architecture-on-premises-only)

**S9**  [Getting started with CCM](https://support.fortanix.com/docs/getting-started-with-fortanix-ccm)

**S10**  [SaaS and on premises feature matrix](https://support.fortanix.com/docs/fortanix-ccm-feature-support-matrix-saas-vs-on-premises)

**S11**  [CCM quickstart](https://support.fortanix.com/docs/fortanix-ccm-quickstart-guide)

**S12**  [Application and compute node policy enforcement](https://support.fortanix.com/docs/fortanix-ccm-application-and-compute-node-policy-enforcement)

**S19**  [CCM 5 point 1 release notes](https://support.fortanix.com/docs/5-1-ccm-october-07-2026)

**S21**  [TCB grace period](https://support.fortanix.com/v1/docs/tcb-grace-period)

**S22**  [Fortanix Confidential Containers FAQ](https://www.fortanix.com/faq/confidential-computing/confidential-containers)

**S24**  [Fortanix Technical Support Program](https://www.fortanix.com/support/technical-support-program)

**T1**  [IETF RFC 9334 remote attestation architecture](https://www.rfc-editor.org/info/rfc9334/)

**T2**  [Confidential Computing Consortium on attestation](https://confidentialcomputing.io/2023/04/06/why-is-attestation-required-for-confidential-computing/)

**T3**  [Intel TDX overview and comparison with SGX](https://www.intel.com/content/www/us/en/support/articles/000097227/processors/intel-xeon-processors.html)

**T4**  [AWS Nitro Enclaves overview](https://docs.aws.amazon.com/enclaves/latest/user/nitro-enclave.html)

**T5**  [NVIDIA confidential computing trust and threat model](https://docs.nvidia.com/confidential-computing/virtual-machines/threat-model)

**T6**  [AMD Secure Encrypted Virtualization](https://www.amd.com/en/developer/sev.html)

**T7**  [IETF TLS version 1 point 3 specification](https://www.rfc-editor.org/info/rfc9846/)

**T10**  [AMD security bulletin on quality of service side channels](https://www.amd.com/en/resources/product-security/bulletin/amd-sb-3035.html)
